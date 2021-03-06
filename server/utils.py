import gzip
import hashlib
import os
import base64
import datetime as dt
import errno

from glob import glob

from flask import jsonify, make_response
from sqlalchemy import and_
from sqlalchemy.exc import IntegrityError

from server import app, db
from server.models import Server, User, Access, Sample, File


CHUNK_PREFIX = 'chunk.'


class InvalidColumnName(Exception):
    pass


class InvalidServerToken(Exception):
    pass


class InvalidFileSize(Exception):
    pass


class DirectoryCreationError(Exception):
    pass


class TruncatedBam(Exception):
    pass


def return_data(data, status_code=200):
    if status_code >= 300:
        app.logger.warning('{} - {}'.format(data, status_code))

    return make_response(jsonify(data), status_code)


def return_message(message, status_code):
    return return_data({'message': message}, status_code=status_code)


def generate_auth_token(server_token, username, name=None, email=None, duration_days=1):
    # Check for a valid server token in the database
    server = Server.query.filter_by(server_token=server_token).first()
    if not server:
        raise InvalidServerToken({"message": "Invalid server token"})

    # Get user from db or create a new one
    user_id = '{}/{}'.format(server.server_id, username)
    user = User.query.filter_by(user_id=user_id).first()
    if not user:
        user = User(user_id=user_id, user_name=name, user_email=email)
        db.session.add(user)
        # Attach the new user to the server
        server.users.append(user)

    auth_token = base64.urlsafe_b64encode(os.urandom(12))
    current_date = dt.datetime.today()
    expiry_date = current_date + dt.timedelta(duration_days)
    access = Access(auth_token=auth_token, creation_date=current_date, expiration_date=expiry_date)
    db.session.add(access)

    # Attach the new token to the user
    user.access.append(access)
    db.session.commit()

    return auth_token, expiry_date


def get_auth_status(auth_token):
    current_time = dt.datetime.today()
    access = Access.query.filter_by(auth_token=auth_token).first()
    if not access:
        return 'not found'
    if current_time > access.expiration_date:
        return 'expired'

    return 'valid'


def get_auth_response(auth_status):
    if auth_status == 'valid':
        return return_message('Success: Valid transfer code', 200)
    elif auth_status == 'expired':
        return return_message('Error: Transfer code has expired', 410)
    elif auth_status == 'not found':
        return return_message('Error: Transfer code does not exist', 404)
    else:
        return return_message('Error: Unexpected authentication status', 500)


def allowed_file(filename):
    suffix = os.path.splitext(filename)[1].lower()
    return suffix in app.config['ALLOWED_EXTENSIONS']


def bam_test(filename):
    bam_eof = \
        '\x1f\x8b\x08\x04\x00\x00\x00\x00\x00\xff\x06\x00BC\x02\x00\x1b\x00\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00'
    with open(filename, 'rb') as f:
        f.seek(-28, 2)
        return f.read() == bam_eof


def validate_bam(filename):
    if not bam_test(filename):
        raise TruncatedBam


def md5_test(checksum, filename):
    md5 = hashlib.md5()
    with open(filename, 'rb') as f:
        for chunk in iter(lambda: f.read(128 * md5.block_size), b''):
            md5.update(chunk)
    return checksum == md5.hexdigest()


def is_gzip_file(filename):
    suffix = os.path.splitext(filename)[1].lower()
    return suffix in ['.gz', '.tgz']


def gzip_test(filename):
    try:
        with gzip.open(filename, 'rb') as f:
            f.read(1024 * 1024)
        return True
    except IOError:
        return False


def get_tempdir(*args):
    path = app.config['UPLOAD_FOLDER']
    for subdir in list(args):
        path = os.path.join(path, subdir)
    return path


def make_tempdir(dirname):
    if not os.path.isdir(dirname):
        try:
            os.makedirs(dirname, 511)  # rwxrwxrwx (octal: 777)
        except OSError as e:
            if e.errno != errno.EEXIST or not os.path.isdir(dirname):
                app.logger.error('Could not create directory: {}\n{}'.format(dirname, e))
                raise DirectoryCreationError


def get_chunk_filename(temp_dir, chunk_number):
    return os.path.join(temp_dir, "{}{:08d}".format(CHUNK_PREFIX, chunk_number))


def get_file_chunks(temp_dir):
    return glob(os.path.join(temp_dir, "{}*".format(CHUNK_PREFIX)))


def merge_chunks(chunk_paths, filename):
    chunk_paths.sort()
    output_file = os.path.join(os.path.dirname(os.path.realpath(chunk_paths[0])), filename)
    try:
        with open(output_file, 'wb') as OUTPUT:
            for path in chunk_paths:
                with open(path, 'rb') as INPUT:
                    OUTPUT.write(INPUT.read())

        app.logger.info('Merged chunks -> %s', output_file)
        # Indicate that file merged successfully
        return True
    except IOError:
        try:
            os.remove(output_file)
        except OSError:
            pass

    return False


def generate_file(data):
    temp_dir = get_tempdir(data['auth_token'], data['identifier'])
    all_chunks = get_file_chunks(temp_dir)

    # Check for all chunks and that the file doesn't already exists
    merged_file = os.path.join(temp_dir, data['filename'])
    if not os.path.isfile(merged_file):
        # Attempt to merge all chunks
        success = merge_chunks(all_chunks, data['filename'])
        if not success:
            update_file_status(data['identifier'], 'unmerged')
            return return_message('Error: File could not be merged', 500)

    # Check for GZIP and perform integrity test
    if is_gzip_file(data['filename']) and not gzip_test(merged_file):
        remove_from_uploads(temp_dir)
        update_file_status(data['identifier'], 'corrupt')
        return return_message('Error: Truncated GZIP file', 415)
    elif os.path.getsize(merged_file) != data['total_size']:
        # Ensure the final file size on disk matches the expected size from the client
        os.remove(merged_file)
        return return_message('Error: Inconsistent merged file size', 415)

    update_file_status(data['identifier'], 'complete')
    return return_message('Success: File upload completed successfully', 200)


def remove_from_uploads(tempdir):
    try:
        all_chunks = os.listdir(tempdir)
        for chunk in all_chunks:
            os.remove(os.path.join(tempdir, chunk))
        os.rmdir(tempdir)
    except OSError:
        pass


def get_user_files(user_id, status):
    files = File.query.filter_by(user_id=user_id, upload_status=status).all()
    db_files = {}
    for file in files:
        db_files[file.identifier] = {
                'identifier': file.identifier,
                # Sample name passed is from the first sample it was originally uploaded with
                'sample-name': file.samples[0].sample_name,
                'filename': file.filename,
                'type': file.file_type,
                'readset': file.readset,
                'platform': file.platform,
                'run-type': file.run_type,
                'capture-kit': file.capture_kit,
                'library': file.library,
                'reference': file.reference
            }
    return db_files


def get_user_by_auth_token(auth_token):
    access = Access.query.filter_by(auth_token=auth_token).first()
    if access:
        return access.user.user_id


def get_or_create_sample(sample_name, user_id):
    sample = Sample.query.filter_by(user_id=user_id, sample_name=sample_name).first()

    if not sample:
        sample = Sample(sample_name=sample_name)
        user = User.query.filter_by(user_id=user_id).first()
        user.samples.append(sample)
        try:
            db.session.add(sample)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            # Sample was added already or in a separate thread, therefore rollback and return the existing sample
            return Sample.query.filter_by(user_id=user_id, sample_name=sample_name).first()

    return sample


def get_or_create_file(data):
    auth_token = data.get('auth_token')

    user = User.query.filter_by(user_id=get_user_by_auth_token(auth_token)).first()
    sample = get_or_create_sample(data.get('sample_name'), user.user_id)
    access = Access.query.filter_by(auth_token=auth_token).first()
    file = File.query.filter_by(identifier=data.get('identifier')).first()
    if not file:
        file = File()

    file.identifier = data.get('identifier')
    file.filename = data.get('filename')
    file.total_size = data.get('total_size')
    file.file_type = data.get('file_type')
    file.user_id = user.user_id
    file.access_id = access.id
    file.readset = data.get('readset')
    file.platform = data.get('platform')
    file.run_type = data.get('run_type')
    file.capture_kit = data.get('capture_kit')
    file.library = data.get('library')
    file.reference = data.get('reference')
    file.upload_status = 'ongoing'
    file.upload_start_date = dt.datetime.today()
    file.is_archived = 0

    # Attach the file to this sample and access objects
    sample.files.append(file)
    access.files.append(file)

    db.session.add(file)
    db.session.commit()

    return file


def update_file_status(identifier, status):
    file = File.query.filter_by(identifier=identifier).first()
    if file:
        # set the file status
        file.upload_status = status
        # set the upload end date for any status other than 'ongoing'
        if status != 'ongoing':
            file.upload_end_date = dt.datetime.today()

        db.session.add(file)
        db.session.commit()


def get_files(server_token, filters=None):
    # Check for a valid server token in the database
    server = Server.query.filter_by(server_token=server_token).first()
    if not server:
        raise InvalidServerToken({"message": "Invalid server token"})

    if filters:
        try:
            files = File.query.filter(and_(File.__dict__[col] == val for col, val in filters.items())).all()
        except KeyError:
            raise InvalidColumnName({"message": "Invalid column name"})
    else:
        files = File.query.all()

    db_files = {}
    for file in files:
        db_files[file.identifier] = {
                'identifier': file.identifier,
                # Sample name passed is from the first sample it was originally uploaded with
                'sample-name': file.samples[0].sample_name,
                'filename': file.filename,
                'total_size': file.total_size,
                'file_type': file.file_type,
                'readset': file.readset,
                'platform': file.platform,
                'run_type': file.run_type,
                'capture_kit': file.capture_kit,
                'library': file.library,
                'reference': file.reference,
                'upload_status': file.upload_status,
                'upload_start_date': file.upload_start_date,
                'upload_end_date': file.upload_end_date,
                'user_id': file.user_id,
                'is_archived': file.is_archived
            }
    return db_files


def update_file(server_token, identifier, column, value):
    # Check for a valid server token in the database
    server = Server.query.filter_by(server_token=server_token).first()
    if not server:
        raise InvalidServerToken({"message": "Invalid server token"})

    file = File.query.filter_by(identifier=identifier).first()

    if file:
        if column not in file.__dict__:
            return return_message('Error: Column does not exist', 400)
        setattr(file, column, value)
        db.session.add(file)
        db.session.commit()
        return return_message('Success: File archive_status updated', 200)
    else:
        return return_message('Error: File does not exist', 404)
