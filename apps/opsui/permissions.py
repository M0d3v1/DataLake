"""Authorization for the internal operator UI.

Two audiences, two checks -- neither is "logged in" alone:

- **Platform operators**: `User.is_staff` (the existing Django flag, not
  a new field). May perform legacy raw-payload storage migrations
  cross-tenant. This is a deliberately blunt, well-understood Django
  convention rather than a bespoke permission model for a small internal
  tool.
- **Organization members**: `apps.orgs.models.Membership.role` for the
  specific organization a view is about. Never resolved from a
  user-supplied schema/org string -- always from `Membership` rows tied
  to `request.user`, or (for the public-schema migration tool) an
  `Organization` the platform operator explicitly picked from a
  server-rendered list.
"""

from django.core.exceptions import PermissionDenied

from apps.orgs.models import Membership, Organization

# "Recommended minimum rules" from the operator-UI spec. Legacy storage
# migrations are platform-operator only -- no Membership role grants it,
# see require_platform_operator (used directly, not via a roles set).
CONTINUATION_ROLES = frozenset(
    {Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.ENGINEER}
)
VIEW_ROLES = frozenset(
    {
        Membership.Role.OWNER,
        Membership.Role.ADMIN,
        Membership.Role.ENGINEER,
        Membership.Role.ANALYST,
    }
)


def is_platform_operator(user) -> bool:
    return bool(user and user.is_authenticated and user.is_staff)


def get_membership(user, organization: Organization) -> Membership | None:
    if not user or not user.is_authenticated:
        return None
    return Membership.objects.filter(user=user, organization=organization).first()


def require_platform_operator(user) -> None:
    if not is_platform_operator(user):
        raise PermissionDenied("this action requires platform-operator access")


def require_org_role(user, organization: Organization, allowed_roles: frozenset[str]) -> Membership:
    """Platform operators are always allowed through (break-glass, same
    posture as Django's own is_staff/is_superuser split) -- everyone else
    needs a Membership in `organization` with a role in `allowed_roles`."""
    if is_platform_operator(user):
        # may be None; operator access doesn't require a membership
        return get_membership(user, organization)
    membership = get_membership(user, organization)
    if membership is None or membership.role not in allowed_roles:
        raise PermissionDenied("you do not have access to this organization")
    return membership


def require_org_member(user, organization: Organization) -> Membership | None:
    """Read-only access: any role, including analyst."""
    if is_platform_operator(user):
        return get_membership(user, organization)
    membership = get_membership(user, organization)
    if membership is None or membership.role not in VIEW_ROLES:
        raise PermissionDenied("you do not have access to this organization")
    return membership
