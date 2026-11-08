from django.db import models
from django.db.models import F, Q

from apps.organizations.models import Organization

DEPARTMENT_NAME_MAX_LENGTH = 200


class Department(models.Model):
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="departments",
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="children",
    )
    name = models.CharField(max_length=DEPARTMENT_NAME_MAX_LENGTH)
    description = models.TextField(blank=True, default="")
    is_archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~Q(parent_id=F("id")),
                name="department_parent_is_not_self",
            ),
            models.UniqueConstraint(
                fields=["organization", "id"],
                name="unique_department_organization_id",
            ),
        ]

    def __str__(self):
        return self.name
