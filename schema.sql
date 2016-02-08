drop table if exists users;
create table users (
	user_id integer primary key autoincrement,
	username varchar not null, 
	login_site varchar not null,
	auth_token varchar not null
);


drop table if exists groups;
create table groups (
	group_id integer primary key autoincrement,
	groupname varchar not null, 
	login_site varchar not null
);


drop table if exists membership;
create table membership (
	user_id integer references users(user_id),
	group_id integer references groups(group_id)
);


drop table if exists files;
create table files (
	file_id integer primary key autoincrement,
	filename varchar not null, 
	owner integer references users(user_id)
);


drop table if exists runs;
create table runs (
	run_id integer primary key autoincrement,
	owner integer references users(user_id),
	status varchar,
	configuration varchar
);


drop table if exists fileruns;
create table fileruns (
    file_id integer references files(file_id),
    run_id integer references runs(run_id)
);


drop table if exists access;
create table access (
    access_id integer primary key autoincrement,
    description varchar,
    level_value integer not null
);


drop table if exists permissions;
create table permissions (
    permission_id integer primary key autoincrement,
    run_id references runs(run_id),
    access_id references access(access_id),
    user_id references users(user_id),
    group_id references groups(group_id)
);