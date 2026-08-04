from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django_tenants.test.cases import TenantTestCase

from apps.orgs.models import Membership

User = get_user_model()


class MembershipTests(TenantTestCase):
    def test_membership_is_unique_per_user_and_organization(self):
        user = User.objects.create_user(
            username="analyst", email="analyst@example.test", password="x"
        )
        Membership.objects.create(user=user, organization=self.tenant, role=Membership.Role.ANALYST)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Membership.objects.create(
                user=user, organization=self.tenant, role=Membership.Role.ENGINEER
            )
