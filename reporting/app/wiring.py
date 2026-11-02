"""Builds the concrete module implementations and injects their dependencies."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass

from reporting.app.config import ConfigError, Settings
from reporting.auth import AuthService, CreateUser, create_auth_service
from reporting.connections import ConnectionService, create_connection_service
from reporting.core import ValidationFailed
from reporting.metadata import MetadataService, create_metadata_service
from reporting.query import QueryService, create_query_service
from reporting.render import RenderService, create_render_service
from reporting.reports import ReportService, create_report_service

log = logging.getLogger("reporting")


@dataclass(frozen=True)
class Services:
    settings: Settings
    connections: ConnectionService
    metadata: MetadataService
    query: QueryService
    render: RenderService
    auth: AuthService
    reports: ReportService


def build_services(settings: Settings) -> Services:
    if settings.app_db_url.startswith("sqlite:///"):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
    connections = create_connection_service(settings.app_db_url, settings.master_key)
    metadata = create_metadata_service(settings.app_db_url, connections)
    query = create_query_service(metadata, connections, cache_url=settings.redis_url,
                                 cache_ttl_s=settings.cache_ttl_s)
    auth = create_auth_service(settings.app_db_url, settings.jwt_secret, settings.session_ttl_s)
    # Embed secrets derive from this key; domain-separated from the session secret.
    embed_key = settings.embed_key or hmac.new(
        settings.jwt_secret.encode(), b"reporting-embeds", hashlib.sha256).hexdigest()
    reports = create_report_service(settings.app_db_url, embed_key)
    services = Services(settings, connections, metadata, query, create_render_service(), auth,
                        reports)
    _bootstrap_admin(services)
    return services


def _bootstrap_admin(services: Services) -> None:
    if services.auth.list_users():
        return
    if services.settings.production and not services.settings.admin_password:
        raise ConfigError(["No users exist yet: set REPORTING_ADMIN_PASSWORD for the first "
                           "administrator (you can remove it after the first start)"])
    password = services.settings.admin_password or secrets.token_urlsafe(12)
    try:
        services.auth.create_user(CreateUser(username=services.settings.admin_username,
                                             password=password, roles=("admin",)))
    except ValidationFailed:
        return  # another server process created the administrator first
    if services.settings.admin_password is None:
        log.warning("Created administrator '%s' with generated password: %s",
                    services.settings.admin_username, password)
