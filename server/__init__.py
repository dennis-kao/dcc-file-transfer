from flask import Flask


app = Flask(__name__)
app.config.from_object('config.base')


# Must go last to avoid circular imports
from server import views
