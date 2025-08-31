from flask import Flask
from routes.main_routes import main_routes
from routes.settings_routes import settings_routes
from tasks import scheduler

app = Flask(__name__)
app.secret_key = "supersecretkey"

app.register_blueprint(main_routes)
app.register_blueprint(settings_routes)

if __name__ == "__main__":
    app.run(debug=True, use_reloader=False)
