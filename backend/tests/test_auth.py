"""Auth route tests against an in-memory database; database.db is never touched."""

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Supply required application settings when no .env is present.
with patch.dict(os.environ, {
    "SECRET_KEY": "test-only-secret-key-at-least-32-bytes-long", "DATABASE_URL": "sqlite://",
}):
    import models  # noqa: F401  registers every model on Base.metadata
    from core.config import settings
    from db.database import Base, get_db
    from main import app

REGISTER = "/api/auth/register"
LOGIN = "/api/auth/login"
ME = "/api/auth/me"
USER = {"username": "testname", "email": "Testname@Example.com", "password": "password123",
        "first_name": "Testname"}


class AuthTests(unittest.TestCase):
    def setUp(self):
        # Fresh in-memory database per test; StaticPool keeps one shared connection
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                               poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        Session = sessionmaker(bind=engine)

        def test_db():
            db = Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = test_db
        self.addCleanup(app.dependency_overrides.clear)
        # Not used as a context manager, so the startup hook (create_tables) doesn't run
        self.client = TestClient(app)

    def register(self, **changes):
        return self.client.post(REGISTER, json={**USER, **changes})

    def login(self, identifier, password="password123"):
        return self.client.post(LOGIN, data={"username": identifier, "password": password})

    def token(self):
        self.register()
        return self.login("testname").json()["access_token"]

    # register

    def test_register_returns_user_without_password(self):
        r = self.register()
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertEqual(body["username"], "testname")
        self.assertEqual(body["email"], "testname@example.com")  # stored lowercase
        self.assertNotIn("password", body)
        self.assertNotIn("hashed_password", body)

    def test_register_duplicate_username_or_email_is_409(self):
        self.register()
        self.assertEqual(self.register(email="new@example.com").status_code, 409)
        self.assertEqual(self.register(username="other", email="TESTNAME@example.com").status_code, 409)

    def test_register_rejects_invalid_input(self):
        for changes in ({"password": "short"}, {"email": "not-an-email"}, {"username": "ab"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.register(**changes).status_code, 422)

    def test_register_limits_match_database_columns(self):
        # The users table allows 25 characters; longer values would crash on Postgres
        self.assertEqual(self.register(username="u" * 26).status_code, 422)
        self.assertEqual(self.register(username="other", email="o@example.com",
                                       first_name="f" * 26).status_code, 422)
        self.assertEqual(self.register(username="u" * 25).status_code, 201)

    # login

    def test_login_with_username_or_email_in_any_case(self):
        self.register()
        for identifier in ("testname", "testname@example.com", "Testname@Example.com"):
            with self.subTest(identifier=identifier):
                r = self.login(identifier)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["token_type"], "bearer")
                self.assertTrue(r.json()["access_token"])

    def test_login_failures_share_one_message(self):
        self.register()
        wrong = self.login("testname", password="wrong-password")
        unknown = self.login("ghost")
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(unknown.status_code, 401)
        # Same message, so the response doesn't reveal which accounts exist
        self.assertEqual(wrong.json(), unknown.json())

    def test_password_is_stored_hashed(self):
        self.register()
        db = next(app.dependency_overrides[get_db]())
        stored = db.query(models.User).one().hashed_password
        self.assertNotEqual(stored, USER["password"])
        self.assertTrue(stored.startswith("$argon2"))

    # protected route

    def test_me_with_valid_token(self):
        r = self.client.get(ME, headers={"Authorization": f"Bearer {self.token()}"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["username"], "testname")

    def test_me_rejects_missing_or_bad_token(self):
        self.assertEqual(self.client.get(ME).status_code, 401)
        bad = self.client.get(ME, headers={"Authorization": "Bearer not-a-token"})
        self.assertEqual(bad.status_code, 401)

    def test_me_rejects_expired_token(self):
        self.register()
        with patch.object(settings, "ACCESS_TOKEN_EXPIRE_MINUTES", -1):
            expired = self.login("testname").json()["access_token"]
        r = self.client.get(ME, headers={"Authorization": f"Bearer {expired}"})
        self.assertEqual(r.status_code, 401)

    def test_me_rejects_token_signed_with_another_key(self):
        token = self.token()
        with patch.object(settings, "SECRET_KEY", "a-different-secret-key-at-least-32-bytes"):
            r = self.client.get(ME, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 401)

    def test_swagger_login_url_matches_route(self):
        spec = self.client.get("/openapi.json").json()
        flow = spec["components"]["securitySchemes"]["OAuth2PasswordBearer"]["flows"]["password"]
        self.assertEqual(flow["tokenUrl"], LOGIN)


if __name__ == "__main__":
    unittest.main()
