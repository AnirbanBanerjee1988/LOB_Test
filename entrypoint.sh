#!/bin/sh
set -e

echo "Running migrations..."
python manage.py migrate --noinput

echo "Ensuring admin superuser exists..."
python manage.py shell -c "from trading.models import BaseUser as U; U.objects.filter(is_superuser=True).exists() or U.objects.create_superuser(user_id='admin', email='admin@example.com', password='admin123', name='Admin'); print('Admin present (default login admin/admin123 only if none existed)')" || echo "WARN: admin bootstrap step failed (see trace above); starting server anyway"

echo "Starting Daphne..."
exec daphne -b 0.0.0.0 -p ${PORT:-8000} trading_system.asgi:application
