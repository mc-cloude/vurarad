import os
import logging
from google.cloud.sql.connector import Connector, IPTypes
import sqlalchemy
from sqlalchemy import create_engine

logger = logging.getLogger("vura-logic")

def get_db_connection():
    """
    Initializes a connection pool for a Cloud SQL instance of Postgres.
    Uses the Cloud SQL Python Connector for secure connectivity.
    """
    instance_connection_name = os.environ.get("INSTANCE_CONNECTION_NAME")
    db_user = os.environ.get("DB_USER")
    db_pass = os.environ.get("DB_PASS")
    db_name = os.environ.get("DB_NAME")
    ip_type = IPTypes.PRIVATE if os.environ.get("PRIVATE_IP") else IPTypes.PUBLIC

    if not all([instance_connection_name, db_user, db_pass, db_name]):
        logger.warning("Cloud SQL Environment variables missing. running in Mock/SQlite mode.")
        return create_engine("sqlite:////tmp/mock.db")

    # Initialize Connector object
    connector = Connector()

    def getconn():
        conn = connector.connect(
            instance_connection_name,
            "pg8000",
            user=db_user,
            password=db_pass,
            db=db_name,
            ip_type=ip_type,
        )
        return conn

    # Create connection pool
    pool = sqlalchemy.create_engine(
        "postgresql+pg8000://",
        creator=getconn,
    )
    return pool
