#!/bin/sh
set -e

echo "Running migrations..."
python manage.py migrate --noinput

echo "Ensuring admin superuser exists..."
python manage.py shell <<'PYEOF'
from trading.models import BaseUser as U
if not U.objects.filter(is_superuser=True).exists():
    U.objects.create_superuser(
        user_id="admin",
        email="admin@example.com",
        password="admin123",
        name="Admin",
    )
    print("Created default admin (admin/admin123)")
else:
    print("Admin already exists; leaving as-is")
PYEOF

echo "Starting Daphne..."
exec daphne -b 0.0.0.0 -p ${PORT:-8000} trading_system.asgi:application
