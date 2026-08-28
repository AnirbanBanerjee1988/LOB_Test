#!/bin/sh
set -e

echo "Running migrations..."
python manage.py migrate --noinput

echo "Ensuring admin superuser exists..."
python manage.py shell -c "from trading.models import BaseUser as U; u,c=U.objects.get_or_create(user_id='admin', defaults={'username':'admin','name':'Admin','email':'admin@example.com','role':'ADMIN','is_staff':True,'is_superuser':True}); u.username='admin'; u.role='ADMIN'; u.is_staff=True; u.is_superuser=True; (u.set_password('admin123') if (c or not u.has_usable_password()) else None); u.save(); print(('Created' if c else 'Verified')+' admin superuser (login: admin / admin123)')" || echo "WARN: admin bootstrap step failed (see trace above); starting server anyway"

echo "Starting Daphne..."
exec daphne -b 0.0.0.0 -p ${PORT:-8000} trading_system.asgi:application
