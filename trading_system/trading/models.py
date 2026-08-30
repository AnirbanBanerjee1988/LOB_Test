from django.db import models
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.utils.timezone import now
from django.core.exceptions import ValidationError
from decimal import Decimal

class CustomUserManager(BaseUserManager):
    def create_user(self, user_id, email=None, password=None, **extra_fields):
        if not user_id:
            raise ValueError("The Registration No field must be set")
        email = self.normalize_email(email)
        extra_fields.setdefault('username', user_id)
        user = self.model(user_id=user_id, email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, user_id, email=None, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('role', 'ADMIN')
        extra_fields.setdefault('name', 'Admin User')
        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')
        return self.create_user(user_id, email, password, **extra_fields)


MARKET_MAKER_INITIAL_CAPITAL = Decimal('5000000.00')  # 50 lakh
TRADER_INITIAL_CAPITAL = Decimal('1000000.00')         # 10 lakh
MARKET_MAKER_INITIAL_INVENTORY = 100
TRADER_INITIAL_INVENTORY = 0


class BaseUser(AbstractUser):
    user_id = models.CharField(max_length=100, unique=True, verbose_name="User ID")
    name = models.CharField(max_length=150)

    ROLE_CHOICES = (
        ('TRADER', 'Trader'),
        ('MARKET_MAKER', 'Market Maker'),
        ('ADMIN', 'Admin'),
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    capital = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    inventory = models.IntegerField(default=0)

    USERNAME_FIELD = 'user_id'
    REQUIRED_FIELDS = ['name', 'email']

    objects = CustomUserManager()

    groups = models.ManyToManyField(
        'auth.Group',
        related_name='base_user_groups',
        blank=True,
    )
    user_permissions = models.ManyToManyField(
        'auth.Permission',
        related_name='base_user_permissions',
        blank=True,
    )

    def save(self, *args, **kwargs):
        if self.pk is None:  # Set initial capital/inventory only on first creation
            if self.role == 'MARKET_MAKER':
                self.capital = MARKET_MAKER_INITIAL_CAPITAL
                self.inventory = MARKET_MAKER_INITIAL_INVENTORY
            elif self.role == 'TRADER':
                self.capital = TRADER_INITIAL_CAPITAL
                self.inventory = TRADER_INITIAL_INVENTORY
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.user_id})"


class Trader(BaseUser):
    class Meta:
        proxy = True

    def allowed_order_modes(self):
        return ['MARKET']


class MarketMaker(BaseUser):
    class Meta:
        proxy = True

    def allowed_order_modes(self):
        return ['LIMIT']


from datetime import datetime


class Order(models.Model):
    ORDER_TYPE_CHOICES = [
        ('BUY', 'Buy'),
        ('SELL', 'Sell'),
    ]

    ORDER_MODE_CHOICES = [
        ('LIMIT', 'Limit'),
        ('MARKET', 'Market'),
    ]

    ROLE_CHOICES = [
        ('TRADER', 'Trader'),
        ('MARKET_MAKER', 'Market Maker'),
    ]

    def clean(self):
        if self.user_role == 'TRADER' and self.order_mode != 'MARKET':
            raise ValidationError("Trader can only place MARKET orders")

        if self.user_role == 'MARKET_MAKER' and self.order_mode != 'LIMIT':
            raise ValidationError("Market Maker can only place LIMIT orders")

        if self.order_mode == 'MARKET' and self.price is not None:
            raise ValidationError("Market orders cannot have price")

        if self.order_mode == 'LIMIT' and self.price is None:
            raise ValidationError("Limit orders must have price")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    user = models.ForeignKey(BaseUser, on_delete=models.CASCADE)
    user_role = models.CharField(max_length=30, choices=ROLE_CHOICES)
    order_type = models.CharField(max_length=10, choices=ORDER_TYPE_CHOICES)
    order_mode = models.CharField(max_length=10, choices=ORDER_MODE_CHOICES)
    quantity = models.IntegerField()
    disclosed = models.IntegerField(default=0)
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    is_matched = models.BooleanField(default=False)
    original_quantity = models.IntegerField(default=0)
    is_ioc = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.order_type} {self.order_mode} Order #{self.id} by {self.user}"


class Trade(models.Model):
    buyer = models.ForeignKey(BaseUser, related_name='buy_trades', on_delete=models.CASCADE)
    seller = models.ForeignKey(BaseUser, related_name='sell_trades', on_delete=models.CASCADE)
    quantity = models.IntegerField()
    price = models.DecimalField(max_digits=10, decimal_places=2)
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Trade #{self.id}: {self.buyer} ⇄ {self.seller} ({self.quantity} @ {self.price})"


class StopOrder(models.Model):
    """
    A stop-limit order for Market Makers.

    When the last traded price crosses target_price, this order is
    converted into a regular LIMIT Order and submitted to the book.

    - BUY stop: triggers when last trade price >= target_price
    - SELL stop: triggers when last trade price <= target_price

    order_mode is always LIMIT (Market Makers can only place limit orders).
    price (the limit price) is always required.
    is_matched is set to True after the order has been triggered — the row
    is kept for audit purposes.
    """

    ORDER_TYPE_CHOICES = [
        ('BUY', 'Buy'),
        ('SELL', 'Sell'),
    ]

    # ORDER_MODE_CHOICES = [
    #     ('BUY', 'Buy'),
    #     ('SELL', 'Sell'),
    # ]

    user = models.ForeignKey(BaseUser, on_delete=models.CASCADE)
    user_role = models.CharField(
        max_length=30,
        choices=BaseUser.ROLE_CHOICES,
        default='MARKET_MAKER',
    )
    order_type = models.CharField(max_length=10, choices=ORDER_TYPE_CHOICES)
    # Always LIMIT — Market Makers only place limit orders.
    # Not exposed as a user-editable field.
    # order_mode = models.CharField(max_length=10, default='LIMIT', editable=False)
    # order_mode = models.CharField(max_length=10, choices=ORDER_MODE_CHOICES)
    order_mode = models.CharField(max_length=10)

    quantity = models.IntegerField()
    disclosed = models.IntegerField(default=0)

    # The price level that triggers this order.
    # target_price = models.DecimalField(max_digits=10, decimal_places=2)

    # The limit price the resulting Order will be placed at.
    # price = models.DecimalField(max_digits=10, decimal_places=2)

    target_price = models.DecimalField(max_digits=10, decimal_places=2)
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    timestamp = models.DateTimeField(auto_now_add=True)

    # False = waiting to trigger. True = already triggered (kept for audit trail).
    is_matched = models.BooleanField(default=False)

    is_ioc = models.BooleanField(default=False)

    # def clean(self):
    #     if self.user_role != 'MARKET_MAKER':
    #         raise ValidationError("Stop orders can only be placed by Market Makers.")
    #     if self.price is None or self.price <= 0:
    #         raise ValidationError("A valid limit price is required for stop orders.")
    #     if self.target_price is None or self.target_price <= 0:
    #         raise ValidationError("A valid trigger price is required for stop orders.")

    def clean(self):
        if self.quantity <= 0:
            raise ValidationError("Quantity must be greater than zero.")
        if self.disclosed <= 0:
            raise ValidationError("Disclosed quantity must be greater than zero.")
        if self.disclosed > self.quantity:
            raise ValidationError("Disclosed quantity cannot exceed total quantity.")
        
        # Enforce Minimum Disclosed Quantity constraint (10%)
        if self.disclosed < (self.quantity * Decimal('0.1')) and self.disclosed != self.quantity:
            if self.quantity >= 10:
                raise ValidationError("Disclosed quantity must be at least 10% of total quantity.")

        # ---- NEW ROLE-BASED LOGIC ----
        if self.user_role == 'MARKET_MAKER':
            self.order_mode = 'LIMIT'
            if self.price is None or self.price <= 0:
                raise ValidationError("Market Makers must provide a valid limit price for stop orders.")
        
        elif self.user_role == 'TRADER':
            self.order_mode = 'MARKET'
            self.price = None  # Market orders don't use a limit price

        if self.target_price is None or self.target_price <= 0:
            raise ValidationError("A valid target price is required for stop orders.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"StopOrder {self.order_type} #{self.id} — trigger @ {self.target_price}, limit @ {self.price}"


class MarketControl(models.Model):
    """Simple singleton model to control market state (paused/unpaused)."""
    paused = models.BooleanField(default=False)
    message = models.CharField(max_length=255, blank=True, null=True)

    def __str__(self):
        return f"MarketControl(paused={self.paused})"