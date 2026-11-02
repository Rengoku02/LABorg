"""What the bench pages keep of their own."""

from django.conf import settings
from django.db import models
from django.utils import timezone


class FrictionReport(models.Model):
    """ "This was awkward": a member's note about a page or a step (plan §7, §11 "Follow-up
    at 3-4 weeks: the top 3 friction fixes"). Only system owners read them, in the admin."""

    lab = models.ForeignKey("core.Lab", on_delete=models.CASCADE, related_name="+")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    page = models.CharField(max_length=200, blank=True)
    text = models.TextField(max_length=2000)
    created_at = models.DateTimeField(default=timezone.now)
    resolved = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d} {self.page or 'LabStock'}: {self.text[:60]}"
