import secrets

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Lab(models.Model):
    """A research group. Tenant data (stock, locations, requests, alerts) hangs off a Lab."""

    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    time_zone = models.CharField(max_length=64, default="UTC")
    # Per-recipient "ask the lab" emails go to every active member; this is only a fallback
    # address shown in the UI (e.g. the lab's mailing list) and is never parsed.
    contact_email = models.EmailField(blank=True)
    # Custodian for shared stock and for items whose owner left (created by bootstrap_lab).
    pool_user = models.OneToOneField(
        "User", on_delete=models.PROTECT, null=True, blank=True, related_name="pool_for_lab"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create(self, email, password, **extra):
        email = self.normalize_email(email).lower() if email else None
        user = self.model(email=email, **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra["is_staff"] = True
        extra["is_superuser"] = True
        return self._create(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    """Every human the lab knows about, including people who can never log in.

    Placeholders stand in for former members who still appear in Quartzy history, and each
    lab has one pool user that holds shared and orphaned stock (plan §7 "People").
    """

    class Kind(models.TextChoices):
        MEMBER = "member", "Member"
        PLACEHOLDER = "placeholder", "Former member (no login)"
        POOL = "pool", "Lab stock pool"

    email = models.EmailField(unique=True, null=True, blank=True)
    full_name = models.CharField(max_length=200)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.MEMBER)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = ["full_name"]

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(kind="member") | Q(is_active=False),
                name="core_user_only_members_can_be_active",
            ),
        ]

    def __str__(self):
        return self.full_name or self.email or f"user {self.pk}"

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.strip().lower()
        else:
            self.email = None
        if self.kind != self.Kind.MEMBER:
            self.is_active = False
        super().save(*args, **kwargs)

    def get_full_name(self):
        return self.full_name

    def get_short_name(self):
        return self.full_name.split(" ")[0] if self.full_name else (self.email or "")


class Membership(models.Model):
    class Role(models.TextChoices):
        MEMBER = "member", "Member"
        CO_OWNER = "co_owner", "Wet-lab co-owner"
        PI = "pi", "PI"
        ADMIN = "admin", "System owner"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        ROTATOR = "rotator", "Rotator / visitor"
        LEAVER = "leaver", "Left the lab"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    lab = models.ForeignKey(Lab, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    start_date = models.DateField(default=timezone.localdate)
    end_date = models.DateField(null=True, blank=True)
    # The Quartzy API can't list roles, so whether someone is a Quartzy Lab Admin (and can
    # therefore mark requests Ordered or cancel other people's requests) is recorded here.
    is_quartzy_lab_admin = models.BooleanField(default=False)
    # A bench champion (plan §4): works the lab-pool claims list (departed members' items,
    # "not mine" claims) with the co-owner.
    is_champion = models.BooleanField(default=False)
    # Lenders are thanked by name in the lab summary only if they opt in (plan §11).
    thank_by_name = models.BooleanField(
        default=False, help_text="May the weekly lab summary thank this person by name?"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "lab"], name="core_membership_unique_user_lab"),
        ]

    def __str__(self):
        return f"{self.user} @ {self.lab} ({self.get_role_display()})"

    def is_current(self, on=None):
        on = on or timezone.localdate()
        if self.status == self.Status.LEAVER:
            return False
        return self.end_date is None or self.end_date >= on


class PersonAlias(models.Model):
    """How a Quartzy identity maps onto a User.

    Resolution order: Quartzy user id, then lower-cased email, then a manual queue
    (``user`` stays empty until someone resolves it in the admin).
    """

    class Kind(models.TextChoices):
        QUARTZY_USER_ID = "quartzy_user_id", "Quartzy user id"
        EMAIL = "email", "Email"

    kind = models.CharField(max_length=20, choices=Kind.choices)
    value = models.CharField(max_length=254)
    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="aliases"
    )
    display_name = models.CharField(max_length=200, blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "person aliases"
        constraints = [
            models.UniqueConstraint(fields=["kind", "value"], name="core_alias_unique_kind_value"),
        ]

    def __str__(self):
        target = self.user or "unresolved"
        return f"{self.get_kind_display()} {self.value} → {target}"


class PolicySetting(models.Model):
    """An override for a tunable from ``apps.core.policies.DEFAULTS``.

    A row with no lab overrides the default for every lab; a lab row wins over both.
    """

    lab = models.ForeignKey(Lab, on_delete=models.CASCADE, null=True, blank=True)
    key = models.CharField(max_length=100)
    value = models.JSONField()
    note = models.CharField(max_length=300, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["lab", "key"], nulls_distinct=False, name="core_policy_unique_lab_key"
            ),
        ]

    def __str__(self):
        return f"{self.key}={self.value!r} ({self.lab or 'all labs'})"


def _new_nonce():
    return secrets.token_urlsafe(16)


class Notification(models.Model):
    """One email to one recipient about one subject.

    The unique constraint is the idempotency key: a retried or re-delivered task can never
    send the same alert twice. Signed links carry the id, recipient and ``nonce``; only the
    nonce is stored, and rotating it revokes every outstanding link (plan §12).
    """

    class Kind(models.TextChoices):
        DUPLICATE_ALERT = "duplicate_alert", "Duplicate alert"
        CLEAR_TO_ORDER = "clear_to_order", "No match — clear to order"
        ASK_OWNER = "ask_owner", "Ask owner"
        ASK_LAB = "ask_lab", "Ask the lab"
        STALE_NUDGE = "stale_nudge", "Stale request nudge"
        CLAIMS = "claims", "Claim your history"
        PERSONAL_DIGEST = "personal_digest", "Personal digest"
        LAB_SUMMARY = "lab_summary", "Lab summary"
        SYSTEM = "system", "System"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        HELD = "held", "Held for review"
        SENT = "sent", "Sent"
        SUPPRESSED = "suppressed", "Suppressed"
        FAILED = "failed", "Failed"

    lab = models.ForeignKey(Lab, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=30, choices=Kind.choices)
    recipient = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="notifications"
    )
    # Always filled, so unmapped Quartzy requesters can still be emailed.
    recipient_email = models.EmailField()
    subject_type = models.ForeignKey(ContentType, on_delete=models.PROTECT)
    subject_id = models.CharField(max_length=64)
    # Separates repeating notifications about the same subject (e.g. "2026-W44").
    period_key = models.CharField(max_length=32, blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    hold_reason = models.CharField(max_length=200, blank=True)
    context = models.JSONField(default=dict, blank=True)
    nonce = models.CharField(max_length=32, default=_new_nonce, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    esp_message_id = models.CharField(max_length=200, blank=True)
    error = models.TextField(blank=True)
    acted_at = models.DateTimeField(null=True, blank=True)
    action = models.CharField(max_length=50, blank=True)
    action_detail = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "subject_type", "subject_id", "recipient_email", "period_key"],
                name="core_notification_idempotency",
            ),
        ]
        indexes = [models.Index(fields=["status", "created_at"])]

    def __str__(self):
        return f"{self.get_kind_display()} → {self.recipient_email}"

    def rotate_nonce(self):
        self.nonce = _new_nonce()
        self.save(update_fields=["nonce"])
