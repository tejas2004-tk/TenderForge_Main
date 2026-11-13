from flask import Flask
from flask_cors import CORS

from config import Config
from routes.health_routes import health_bp
from routes.test_routes import test_bp
from routes.auth_routes import auth_bp
from routes.company_profile_routes import company_profile_bp
from routes.tender_routes import tender_bp
from routes.tender_analysis_routes import analysis_bp


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    app.config["MAX_CONTENT_LENGTH"] = 52 * 1024 * 1024
    allowed_origins = [app.config["FRONTEND_ORIGIN"], "http://127.0.0.1:5173"]
    CORS(app, resources={r"/api/*": {"origins": allowed_origins}}, supports_credentials=True)
    app.register_blueprint(health_bp, url_prefix="/api")
    app.register_blueprint(test_bp, url_prefix="/api")
    app.register_blueprint(auth_bp, url_prefix="/api/auth")
    app.register_blueprint(company_profile_bp, url_prefix="/api/company-profile")
    app.register_blueprint(tender_bp, url_prefix="/api/tenders")
    app.register_blueprint(analysis_bp, url_prefix="/api/tenders")
    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=app.config["PORT"], debug=app.config["DEBUG"])
