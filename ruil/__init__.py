import os
from datetime import timedelta
from zoneinfo import ZoneInfo

import click
from flask import Flask
from werkzeug.security import generate_password_hash

from .store import Store
from .sync import run_sync


def create_app(overrides=None):
    app = Flask(__name__)
    env = os.environ
    app.config.update(
        SECRET_KEY=env.get("SECRET_KEY", "dev-only-change-me"),
        DATA_DIR=env.get("DATA_DIR", os.path.join(os.getcwd(), "data")),
        SPOND_USERNAME=env.get("SPOND_USERNAME", ""),
        SPOND_PASSWORD=env.get("SPOND_PASSWORD", ""),
        SPOND_GROUP_ID=env.get("SPOND_GROUP_ID", ""),
        SPOND_SUBGROUP_ID=env.get("SPOND_SUBGROUP_ID", ""),
        TZ=ZoneInfo(env.get("APP_TIMEZONE", "Europe/Amsterdam")),
        SYNC_WEEKS=int(env.get("SYNC_WEEKS", 8)),
        SYNC_MINUTES=int(env.get("SYNC_MINUTES", 30)),
        ESCALATE_DAYS=int(env.get("ESCALATE_DAYS", 7)),
        LATE_AFTER_DAYS=int(env.get("LATE_AFTER_DAYS", 2)),
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_HTTPONLY=True,
    )
    if overrides:
        app.config.update(overrides)
    if "DEMO" not in app.config:
        app.config["DEMO"] = not (app.config["SPOND_USERNAME"] and app.config["SPOND_GROUP_ID"])

    app.extensions["store"] = Store(app.config["DATA_DIR"])

    from . import views
    app.register_blueprint(views.bp)
    register_cli(app)
    return app


def register_cli(app):
    store = app.extensions["store"]

    @app.cli.command("sync")
    def sync_cmd():
        """Pull trainings and hosts from Spond (or demo data)."""
        n = run_sync(app.config, store)
        click.echo(f"Synced ({'demo' if app.config['DEMO'] else 'spond'}); {n} swap(s) marked processed.")

    @app.cli.command("add-user")
    @click.argument("username")
    @click.option("--name", required=True, help="Display name")
    @click.option("--email", default="", help="Spond e-mail, used to link the user to Spond")
    @click.option("--spond-id", default="", help="Spond profile id (overrides e-mail matching)")
    @click.option("--admin", is_flag=True)
    @click.password_option()
    def add_user(username, name, email, spond_id, admin, password):
        """Create or update a user."""
        def put(users):
            users[username.lower()] = {
                "name": name, "email": email.lower(), "spond_id": spond_id or None,
                "admin": admin, "password_hash": generate_password_hash(password),
            }
        store.update("users", {}, put)
        click.echo(f"Saved user {username}.")

    @app.cli.command("seed-demo")
    def seed_demo():
        """Demo users (password 'demo'; pieter is admin) + demo Spond data."""
        from .sync import DEMO_PEOPLE

        if not app.config["DEMO"]:
            raise click.ClickException("Spond is configured; seed-demo only runs in demo mode.")

        def put(users):
            for n in DEMO_PEOPLE:
                users[n.lower()] = {
                    "name": n, "email": f"{n.lower()}@example.org", "spond_id": None,
                    "admin": n == "Pieter", "password_hash": generate_password_hash("demo"),
                }
        store.update("users", {}, put)
        run_sync(app.config, store)
        click.echo("Demo users created (password: demo). Log in as 'pieter' (admin).")
