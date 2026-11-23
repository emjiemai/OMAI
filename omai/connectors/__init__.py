"""Connectors to SAP Business One, 1C and Billz.

Each connector reads (with its own read-only credentials), maps what it reads onto OMAI ids, and hands it to
``omai.connectors.ingest``. Only the *authoritative* source of a location books movements there
(``Location.movement_source``); every other system is compared, so one physical event is never counted twice.
"""
