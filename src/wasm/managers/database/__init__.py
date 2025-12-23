# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Database managers package for WASM.

Provides managers for different database engines:
- MySQL/MariaDB
- PostgreSQL
- Redis
- MongoDB
"""

from wasm.managers.database.base import BaseDatabaseManager, DatabaseInfo, UserInfo
from wasm.managers.database.registry import DatabaseRegistry, get_db_manager
from wasm.managers.database.mysql import MySQLManager
from wasm.managers.database.postgres import PostgresManager
from wasm.managers.database.redis import RedisManager
from wasm.managers.database.mongodb import MongoDBManager

__all__ = [
    "BaseDatabaseManager",
    "DatabaseInfo",
    "UserInfo",
    "DatabaseRegistry",
    "get_db_manager",
    "MySQLManager",
    "PostgresManager",
    "RedisManager",
    "MongoDBManager",
]
