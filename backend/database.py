from threading import Lock

from pymongo import MongoClient

_client = None
_lock = Lock()


def get_database(app):
    """Create the Mongo client only when needed and reuse it afterwards."""
    global _client
    if not app.config["MONGODB_URI"]:
        raise RuntimeError("MongoDB is not configured. Set MONGODB_URI in backend/.env.")

    if _client is None:
        with _lock:
            if _client is None:
                _client = MongoClient(
                    app.config["MONGODB_URI"],
                    serverSelectionTimeoutMS=5000,
                    connectTimeoutMS=5000,
                )
    return _client[app.config["MONGODB_DATABASE"]]
