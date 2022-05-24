# SPDX-FileCopyrightText: 2019-2020 Magenta ApS
#
# SPDX-License-Identifier: MPL-2.0
"""Update logic."""
import re
from functools import cache
from operator import itemgetter
from typing import Pattern
from uuid import UUID

import structlog
from gql import gql
from gql.client import AsyncClientSession
from more_itertools import one
from raclients.graph.client import GraphQLClient
from raclients.modelclient.mo import ModelClient
from ramodels.mo import OrganisationUnit

from .config import get_settings
from .config import Settings

logger = structlog.get_logger()


@cache
async def fetch_org_unit_hierarchy_uuid(session: AsyncClientSession) -> UUID:
    """Fetch the UUID of the 'org_unit_hierarchy' facet.

    Args:
        session: The GraphQL session to run our queries on.

    Returns:
        The UUID of 'org_unit_hierarchy'.
    """
    # TODO: Optimize with better filters in MO
    # Having user-key filters would help a lot

    # Fetch all facets to find org_unit_hierarchy's UUID
    query = gql(
        """
        query FacetQuery {
            facets {
                uuid
                user_key
            }
        }
        """
    )
    result = await session.execute(query)
    # Construct a user-key to uuid map of all facets
    facet_map = dict(map(itemgetter("user_key", "uuid"), result["facets"]))
    org_unit_hierarchy_uuid = facet_map["org_unit_hierarchy"]
    return UUID(org_unit_hierarchy_uuid)


@cache
async def fetch_org_unit_hierarchy_class_uuid(
    session: AsyncClientSession, org_unit_hierarchy_uuid: UUID, class_user_key: str
) -> UUID:
    """Fetch the UUID of the given class within the 'org_unit_hierarchy' facet.

    Args:
        session: The GraphQL session to run our queries on.
        class_user_key: User-key of the class to find UUID for.

    Returns:
        The UUID of class.
    """
    # TODO: Optimize with better filters in MO
    # Having user-key filters would help a lot, so would facet filter on classes.

    # Fetch all classes under org_unit_hierarchy to find the class's UUID
    query = gql(
        """
        query ClassQuery($uuids: [UUID!]) {
            facets(uuids: $uuids) {
                classes {
                    uuid
                    user_key
                }
            }
        }
        """
    )
    result = await session.execute(query, {"uuids": [str(org_unit_hierarchy_uuid)]})
    # Construct a user-key to uuid map of all classes
    class_map = dict(
        map(itemgetter("user_key", "uuid"), one(result["facets"])["classes"])
    )
    class_uuid = class_map[class_user_key]
    return UUID(class_uuid)


@cache
def ny_regex() -> Pattern:
    """Compile 'NYx-niveau' regex.

    Returns:
        Regex matching 'NYx-niveau', with 'x' being an integer.
    """
    return re.compile(r"NY\d-niveau")


async def is_line_management(session: AsyncClientSession, uuid: UUID) -> bool:
    """Determine whether the organisation is part of line management.

    Args:
        session: The GraphQL session to run our queries on.
        uuid: UUID of the organisation unit.

    Returns:
        Whether the organisation unit should be part of line management.
    """
    query = gql(
        """
        query OrgUnitQuery($uuids: [UUID!]) {
            org_units(uuids: $uuids) {
                objects {
                    org_unit_level {
                        user_key
                    }
                    engagements {
                        uuid
                    }
                    associations {
                        uuid
                    }
                }
            }
        }
        """
    )
    result = await session.execute(query, {"uuids": [str(uuid)]})
    logger.debug("GraphQL result", result=result)
    obj = one(one(result["org_units"])["objects"])
    logger.debug("GraphQL obj", obj=obj)

    unit_level_user_key = obj["org_unit_level"]["user_key"]

    # Part of line management if userkey matches regex
    if ny_regex().fullmatch(unit_level_user_key) is not None:
        return True
    # Or if it is "Afdelings-niveau" and it has people attached
    if unit_level_user_key == "Afdelings-niveau":
        # TODO: Check owners, leaders, it?
        if len(obj["engagements"]) > 0:
            return True
        if len(obj["associations"]) > 0:
            return True
    return False


async def fetch_org_unit(session: AsyncClientSession, uuid: UUID) -> OrganisationUnit:
    """Fetch an organisation unit from MO using GraphQL.

    Args:
        session: The GraphQL session to run our queries on.
        uuid: UUID of the organisation to fetch.

    Returns:
        The organisation object.
    """
    query = gql(
        """
        query OrgUnitQuery($uuids: [UUID!]) {
            org_units(uuids: $uuids) {
                objects {
                    uuid
                    user_key
                    validity {
                        from
                        to
                    }
                    name
                    parent_uuid
                    org_unit_hierarchy_uuid: org_unit_hierarchy
                    org_unit_type_uuid: unit_type_uuid
                    org_unit_level_uuid
                }
            }
        }
        """
    )
    logger.debug("Fetching org-unit via GraphQL", uuid=uuid)
    result = await session.execute(query, {"uuids": [str(uuid)]})
    logger.debug("GraphQL result", result=result)
    obj = one(one(result["org_units"])["objects"])
    logger.debug("GraphQL obj", obj=obj)
    obj["from_date"] = obj["validity"]["from"]
    obj["to_date"] = obj["validity"]["to"]
    del obj["validity"]
    org_unit = OrganisationUnit.from_simplified_fields(**obj)
    logger.debug("Organisation Unit", org_unit=org_unit)
    return org_unit


async def should_hide(
    session: AsyncClientSession, uuid: UUID, hidden: list[str]
) -> bool:
    """Determine whether the organisation unit should be hidden.

    Args:
        session: The GraphQL session to run our queries on.
        org_unit: The organisation unit object.
        hidden: User-keys of organisation units to hide (all children included).

    Returns:
        Whether the organisation unit should be hidden.
    """
    # TODO: Should we really just be updating the top-most parent itself?
    if not hidden:
        logger.debug("should_hide called without hidden list")
        return False

    query = gql(
        """
        query ParentQuery($uuids: [UUID!]) {
            org_units(uuids: $uuids) {
                objects {
                    user_key
                    parent_uuid
                }
            }
        }
        """
    )
    result = await session.execute(query, {"uuids": [str(uuid)]})
    logger.debug("GraphQL result", result=result)
    obj = one(one(result["org_units"])["objects"])
    logger.debug("GraphQL obj", obj=obj)

    if obj["user_key"] in hidden:
        return True
    if obj["parent_uuid"] is not None:
        return await should_hide(session, obj["parent_uuid"], hidden)
    return False


async def get_line_management_uuid(
    session: AsyncClientSession, settings: Settings
) -> UUID:
    """Get the UUID of the line_management class.

    Args:
        session: The GraphQL session to run our queries on (if required).
        settings: The pydantic settings module.

    Returns:
        The UUID of class.
    """
    line_management_uuid = settings.line_management_uuid
    if line_management_uuid is None:
        org_unit_hierarchy_uuid = await fetch_org_unit_hierarchy_uuid(session)
        line_management_uuid = await fetch_org_unit_hierarchy_class_uuid(
            session, org_unit_hierarchy_uuid, settings.line_management_user_key
        )
        logger.debug(
            "Line management uuid not set, fetched",
            user_key=settings.line_management_user_key,
            uuid=line_management_uuid,
        )
    return line_management_uuid


async def get_hidden_uuid(session: AsyncClientSession, settings: Settings) -> UUID:
    """Get the UUID of the hidden class.

    Args:
        session: The GraphQL session to run our queries on (if required).
        settings: The pydantic settings module.

    Returns:
        The UUID of class.
    """
    hidden_uuid = settings.hidden_uuid
    if hidden_uuid is None:
        org_unit_hierarchy_uuid = await fetch_org_unit_hierarchy_uuid(session)
        hidden_uuid = await fetch_org_unit_hierarchy_class_uuid(
            session, org_unit_hierarchy_uuid, settings.hidden_user_key
        )
        logger.debug(
            "Hidden uuid not set, fetched",
            user_key=settings.hidden_user_key,
            uuid=hidden_uuid,
        )
    return hidden_uuid


async def update_line_management(uuid: UUID) -> bool:
    """Update line management information for the provided organisation.

    Args:
        UUID of the organisation to recalculate.

    Returns:
        Whether an update was made.
    """
    settings = get_settings()
    client = GraphQLClient(
        url=settings.mo_url + "/graphql",
        client_id=settings.client_id,
        client_secret=settings.client_secret.get_secret_value(),
        auth_server=settings.auth_server,
        auth_realm=settings.auth_realm,
    )
    async with client as session:
        # Determine the desired org_unit_hierarchy class uuid
        new_org_unit_hierarchy: UUID | None = None
        if await should_hide(session, uuid, settings.hidden):
            logger.debug("Organisation Unit needs to be hidden", uuid=uuid)
            new_org_unit_hierarchy = await get_hidden_uuid(session, settings)
        elif await is_line_management(session, uuid):
            logger.debug("Organisation Unit needs to be in line management", uuid=uuid)
            new_org_unit_hierarchy = await get_line_management_uuid(session, settings)

        # Fetch the current object and see if we need to update it
        org_unit = await fetch_org_unit(session, uuid)
        if org_unit.org_unit_hierarchy == new_org_unit_hierarchy:
            logger.debug("Not updating org_unit_hierarchy, already good", uuid=uuid)
            return False

        # Prepare the updated object for writing
        org_unit = org_unit.copy(
            update={"org_unit_hierarchy_uuid": new_org_unit_hierarchy}
        )

    if settings.dry_run:
        logger.info("dry-run: Would have send edit payload", org_unit=org_unit)
        return True

    async with ModelClient(
        base_url=settings.mo_url,
        client_id=settings.client_id,
        client_secret=settings.client_secret.get_secret_value(),
        auth_server=settings.auth_server,
        auth_realm=settings.auth_realm,
    ) as client:
        logger.debug("Sending ModelClient edit request", org_unit=org_unit)
        response = await client.edit([org_unit])
        logger.debug("ModelClient response", response=response)
        return True