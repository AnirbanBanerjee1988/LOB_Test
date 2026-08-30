from django.shortcuts import render, redirect
from .models import (
    BaseUser, Trader, MarketMaker, Order, Trade, MarketControl,
    MARKET_MAKER_INITIAL_CAPITAL, TRADER_INITIAL_CAPITAL,
    MARKET_MAKER_INITIAL_INVENTORY, TRADER_INITIAL_INVENTORY,
)
from django.db.models import Q
from django.db import transaction
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import ensure_csrf_cookie
from decimal import Decimal, ROUND_HALF_UP
import json
import logging
import re
import csv
import io
from django.contrib import messages
from .forms import UserRegisterForm
from .utils import broadcast_orderbook_update
from django.contrib.auth import logout as auth_logout, authenticate, login as auth_login, update_session_auth_hash
from .utils import match_order
from django.http import JsonResponse, HttpResponse
from django.contrib.auth import get_user_model

logger = logging.getLogger(__name__)

User = get_user_model()
AuthUser = User


def _visible_disclosed(order):
    if not order:
        return 0
    peak_disclosed = max(int(order.disclosed or 0), 0)
    quantity = max(int(order.quantity or 0), 0)
    original_quantity = max(int(order.original_quantity or 0), 0)

    if peak_disclosed <= 0:
        return quantity
    if quantity <= 0:
        return 0

    filled = max(original_quantity - quantity, 0)
    consumed_in_current_tranche = filled % peak_disclosed
    current_tranche_visible = peak_disclosed if consumed_in_current_tranche == 0 else (peak_disclosed - consumed_in_current_tranche)

    return min(quantity, current_tranche_visible)


def _serialize_order(order):
    return {
        'user': order.user_id,
        'price': order.price,
        'disclosed': _visible_disclosed(order),
        'is_matched': order.is_matched,
        'id': order.id,
        'is_ioc': order.is_ioc,
        'quantity': order.quantity,
        'original_quantity': order.original_quantity,
    }


def _annotate_trade_sides(user, trades):
    """Attach a per-user Side (BUY/SELL) and counterparty to each of the user's trades."""
    annotated = []
    for t in trades:
        if t.buyer_id == user.id:
            t.side = 'BUY'
            t.counterparty = t.seller.user_id
        else:
            t.side = 'SELL'
            t.counterparty = t.buyer.user_id
        annotated.append(t)
    return annotated


def login(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')

        user = authenticate(request, username=username, password=password)
        if user is not None:
            auth_login(request, user)
            if user.role == 'TRADER':
                return redirect('trader_home')
            elif user.role == 'MARKET_MAKER':
                return redirect('mm_home')
            elif user.role == 'ADMIN':
                return redirect('admin_home')
        else:
            return render(request, 'trading/login.html', {'form': {'errors': True}})

    return render(request, 'trading/login.html')


def logout_view(request):
    auth_logout(request)
    return redirect('login')


def _get_or_create_base_user(auth_user):
    try:
        return BaseUser.objects.get(username=auth_user.username)
    except BaseUser.DoesNotExist:
        if auth_user.is_superuser:
            return BaseUser.objects.create(username=auth_user.username, role='ADMIN')
    return None


def _is_admin(auth_user):
    if not auth_user or auth_user.is_anonymous:
        return False
    try:
        fresh = BaseUser.objects.get(pk=auth_user.pk)
        return fresh.role == 'ADMIN' or fresh.is_superuser
    except BaseUser.DoesNotExist:
        return False


@login_required
def role_router(request):
    auth_user = request.user
    base_user = _get_or_create_base_user(auth_user)
    if not base_user:
        messages.error(request, 'Account role is missing. Please contact an admin.')
        return redirect('login')

    if base_user.role == 'ADMIN':
        if auth_user.is_superuser:
            return redirect('admin_home')
        messages.error(request, 'Admin access requires a superuser account.')
        return redirect('login')
    if base_user.role == 'MARKET_MAKER':
        return redirect('mm_home')
    if base_user.role == 'TRADER':
        return redirect('trader_home')
    return redirect('admin_home')


def _participant_positions():
    """Cash + inventory + mark-to-market P&L for every trader / market maker."""
    last_trade = Trade.objects.order_by('-timestamp').first()
    mark = last_trade.price if last_trade else Decimal('0.00')

    rows = []
    for u in BaseUser.objects.filter(role__in=['TRADER', 'MARKET_MAKER']).order_by('role', 'name'):
        if u.role == 'MARKET_MAKER':
            init_cap, init_inv = MARKET_MAKER_INITIAL_CAPITAL, MARKET_MAKER_INITIAL_INVENTORY
        else:
            init_cap, init_inv = TRADER_INITIAL_CAPITAL, TRADER_INITIAL_INVENTORY

        equity = u.capital + (u.inventory * mark)
        pnl = (u.capital - init_cap) + (u.inventory - init_inv) * mark
        rows.append({
            'user_id': u.user_id,
            'name': u.name,
            'role': u.role,
            'capital': u.capital,
            'inventory': u.inventory,
            'equity': equity,
            'pnl': pnl,
        })
    return rows, mark


@login_required
@ensure_csrf_cookie
def admin_home(request):
    if not _is_admin(request.user):
        return redirect('role_router')

    best_bid = fetch_best_bid()
    best_ask = fetch_best_ask()

    best_bid_price = float(best_bid['price']) if best_bid and best_bid.get('price') is not None else None
    best_ask_price = float(best_ask['price']) if best_ask and best_ask.get('price') is not None else None
    spread = None
    if best_bid_price is not None and best_ask_price is not None and best_ask_price >= best_bid_price:
        spread = best_ask_price - best_bid_price

    recent_trades = Trade.objects.select_related('buyer', 'seller').order_by('-timestamp')[:10]
    participants, mark_price = _participant_positions()

    context = {
        'base_role': 'ADMIN',
        'trader_count': BaseUser.objects.filter(role='TRADER').count(),
        'market_maker_count': BaseUser.objects.filter(role='MARKET_MAKER').count(),
        'active_limit_orders': Order.objects.filter(order_mode='LIMIT', is_matched=False).count(),
        'trades_today': Trade.objects.filter(timestamp__date=timezone.now().date()).count(),
        'total_trades': Trade.objects.count(),
        'best_bid_price': best_bid_price,
        'best_ask_price': best_ask_price,
        'spread': spread,
        'best_bid_disclosed': best_bid['disclosed'] if best_bid else None,
        'best_ask_disclosed': best_ask['disclosed'] if best_ask else None,
        'last_trade': Trade.objects.order_by('-timestamp').first(),
        'recent_trades': recent_trades,
        'participants': participants,
        'mark_price': mark_price,
    }
    return render(request, 'trading/admin.html', context)


@login_required
def get_market_status(request):
    if request.method == 'GET':
        try:
            mc = MarketControl.objects.first()
            paused = mc.paused if mc else False
            message = mc.message if mc else ''
        except Exception:
            paused = False
            message = ''
        return JsonResponse({'paused': paused, 'message': message})
    return JsonResponse({'paused': False, 'message': ''}, status=405)


@login_required
def toggle_market_pause(request):
    if not _is_admin(request.user):
        return JsonResponse({'success': False, 'message': 'Admin access required.'}, status=403)
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            action = data.get('action')
            message = data.get('message', '')
            mc, _ = MarketControl.objects.get_or_create(id=1)
            if action == 'pause':
                mc.paused = True
                mc.message = message
            else:
                mc.paused = False
                mc.message = ''
            mc.save()

            try:
                from asgiref.sync import async_to_sync
                from channels.layers import get_channel_layer
                channel_layer = get_channel_layer()
                async_to_sync(channel_layer.group_send)(
                    'orderbook_group',
                    {
                        'type': 'send_order_update',
                        'payload': {
                            'event': 'market_pause',
                            'paused': mc.paused,
                            'message': mc.message,
                        }
                    }
                )
            except Exception:
                pass

            return JsonResponse({'success': True, 'paused': mc.paused, 'message': mc.message})
        except Exception as e:
            return JsonResponse({'success': False, 'message': str(e)}, status=500)
    return JsonResponse({'success': False}, status=405)


def fetch_best_ask():
    order = Order.objects.filter(
        order_type="SELL", order_mode="LIMIT", price__isnull=False, is_matched=False,
    ).order_by('price').first()
    if not order:
        return None
    return {'price': order.price, 'disclosed': _visible_disclosed(order)}


def fetch_best_bid():
    order = Order.objects.filter(
        order_type="BUY", order_mode="LIMIT", price__isnull=False, is_matched=False,
    ).order_by('-price').first()
    if not order:
        return None
    return {'price': order.price, 'disclosed': _visible_disclosed(order)}


@login_required
def get_best_ask(request):
    if request.method == 'GET':
        return JsonResponse({'best_ask': fetch_best_ask()})
    return JsonResponse({'best_ask': None})


@login_required
def get_best_bid(request):
    if request.method == 'GET':
        return JsonResponse({'best_bid': fetch_best_bid()})
    return JsonResponse({'best_bid': None})


@login_required
def market_maker_home(request):
    auth_user = request.user
    user = _get_or_create_base_user(auth_user)

    if not user or user.role != "MARKET_MAKER":
        return redirect('role_router')

    if request.method == "POST":
        mc = MarketControl.objects.first()
        if mc and mc.paused:
            return JsonResponse({'success': False, 'message': 'Market activity is paused: ' + (mc.message or 'No reason provided.')}, status=403)
        try:
            order_type = request.POST.get('order_type')
            order_mode = 'LIMIT'

            try:
                quantity = int(request.POST.get('quantity', 0))
            except (ValueError, TypeError):
                return JsonResponse({'success': False, 'message': 'Quantity must be a valid integer.'}, status=400)

            if quantity <= 0:
                return JsonResponse({'success': False, 'message': 'Quantity must be an integer greater than 0.'}, status=400)

            raw_disclosed = request.POST.get('disclosed_quantity', '').strip()
            if not raw_disclosed:
                return JsonResponse({'success': False, 'message': 'Disclosed quantity is required.'}, status=400)

            try:
                disclosed = int(raw_disclosed)
            except (ValueError, TypeError):
                return JsonResponse({'success': False, 'message': 'Disclosed quantity must be a valid integer.'}, status=400)

            if disclosed <= 0:
                return JsonResponse({'success': False, 'message': 'Disclosed quantity must be an integer greater than 0.'}, status=400)

            paired_quantity = request.POST.get('paired_quantity')
            is_ioc = request.POST.get('is_ioc') == 'True'
            original_quantity = quantity

            if not order_type or quantity <= 0:
                return JsonResponse({'success': False, 'message': 'Invalid order type or quantity'}, status=400)

            if paired_quantity not in (None, ''):
                try:
                    paired_quantity_value = int(paired_quantity)
                except (TypeError, ValueError):
                    return JsonResponse({'success': False, 'message': 'Invalid paired quantity value.'}, status=400)
                if paired_quantity_value <= 0:
                    return JsonResponse({'success': False, 'message': 'Paired quantity must be greater than 0.'}, status=400)

            min_disclosed = max(1, int(quantity * 0.1))
            if disclosed == 0:
                disclosed = quantity
            if disclosed > quantity:
                disclosed = quantity
            if disclosed < min_disclosed and disclosed >= min_disclosed - 1:
                disclosed = min_disclosed
            if disclosed < min_disclosed:
                return JsonResponse({'success': False, 'message': f'Disclosed quantity too small. Need at least {min_disclosed}.'}, status=400)

            raw_price = request.POST.get('price', '0')
            try:
                if raw_price not in (None, ''):
                    price = Decimal(str(raw_price)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                else:
                    price = None
            except (ValueError, TypeError, Exception):
                return JsonResponse({'success': False, 'message': 'Invalid price format'}, status=400)

            if price is None or price <= 0:
                return JsonResponse({'success': False, 'message': 'Valid price required for limit orders'}, status=400)

            # Capital and inventory checks
            if order_type == 'BUY':
                required_capital = price * quantity
                user.refresh_from_db()
                if user.capital < required_capital:
                    return JsonResponse({
                        'success': False,
                        'message': f'Insufficient capital. Required: ₹{required_capital:,.2f}, Available: ₹{user.capital:,.2f}',
                    }, status=400)
            elif order_type == 'SELL':
                user.refresh_from_db()
                if user.inventory < quantity:
                    return JsonResponse({
                        'success': False,
                        'message': f'Insufficient inventory. Required: {quantity} units, Available: {user.inventory} units.',
                    }, status=400)

            try:
                with transaction.atomic():
                    new_order = Order(
                        order_type=order_type,
                        order_mode=order_mode,
                        quantity=quantity,
                        disclosed=disclosed,
                        price=price,
                        is_matched=False,
                        is_ioc=is_ioc,
                        user=user,
                        user_role=user.role,
                        original_quantity=original_quantity
                    )
                    new_order.save()
                    broadcast_orderbook_update()
                    return JsonResponse({'success': True, 'message': f'Order placed: {order_type} {quantity}@{price}'})
            except Exception as e:
                return JsonResponse({'success': False, 'message': f'Error saving order: {str(e)}'}, status=500)

        except Exception as e:
            return JsonResponse({'success': False, 'message': f'Unexpected error: {str(e)}'}, status=500)

    orders = Order.objects.filter(user=user)
    trades = _annotate_trade_sides(user, list(Trade.objects.filter(Q(buyer=user) | Q(seller=user)).order_by('-timestamp')))

    user.refresh_from_db()
    return render(request, 'trading/market-maker.html', {
        'orders': orders,
        'trades': trades,
        'base_role': user.role,
        'capital': user.capital,
        'inventory': user.inventory,
    })


@login_required
def trader_home(request):
    auth_user = request.user
    user = _get_or_create_base_user(auth_user)

    if not user or user.role != "TRADER":
        return redirect('role_router')

    if request.method == "POST":
        mc = MarketControl.objects.first()
        if mc and mc.paused:
            messages.error(request, 'Market activity is paused.')
            return redirect('trader_home')

        order_type = request.POST.get('order_type')
        order_mode = "MARKET"
        quantity = int(request.POST.get('quantity'))
        disclosed = int(request.POST.get('disclosed_quantity', quantity))
        is_ioc = request.POST.get('is_ioc') == 'True'
        original_quantity = quantity

        if disclosed == 0:
            disclosed = quantity
        if disclosed > quantity:
            disclosed = quantity

        if disclosed < 0.1 * quantity:
            messages.error(request, "Disclosed Quantity cannot be less than 10% of Quantity.")
            return redirect('trader_home')

        # Pre-trade risk checks
        if order_type == 'SELL':
            # Inventory check for SELL orders
            user.refresh_from_db()
            if user.inventory < quantity:
                messages.error(request, f'Insufficient inventory. Required: {quantity} units, Available: {user.inventory} units.')
                return redirect('trader_home')
        elif order_type == 'BUY':
            # Capital check for market BUY orders. A market buy sweeps the resting
            # ask book best-price-first, draining each order fully before moving on,
            # so the exact spend is deterministic: walk the book and total it. We
            # only count the fillable portion (a thin book fills less and spends
            # less), and reject if that spend exceeds available capital.
            user.refresh_from_db()
            resting_asks = Order.objects.filter(
                order_type='SELL', order_mode='LIMIT', price__isnull=False, is_matched=False,
            ).order_by('price', 'timestamp')
            remaining = quantity
            required_capital = Decimal('0.00')
            for ask in resting_asks:
                if remaining <= 0:
                    break
                take = min(remaining, ask.quantity)
                required_capital += ask.price * take
                remaining -= take
            if required_capital > user.capital:
                messages.error(request, f'Insufficient capital. Required: ₹{required_capital:,.2f}, Available: ₹{user.capital:,.2f}')
                return redirect('trader_home')

        try:
            new_order = Order(
                order_type=order_type,
                order_mode=order_mode,
                quantity=quantity,
                disclosed=disclosed,
                price=None,
                is_matched=False,
                is_ioc=is_ioc,
                user=user,
                user_role=user.role,
                original_quantity=original_quantity
            )
            new_order.save()
            broadcast_orderbook_update()
            match_order(new_order)
            messages.success(request, 'Your market order has been placed successfully!')
            return redirect('trader_home')

        except Exception as e:
            messages.error(request, f"Error processing order: {e}")
            return redirect('trader_home')

    orders = Order.objects.filter(user=user)
    trades = _annotate_trade_sides(user, list(Trade.objects.filter(Q(buyer=user) | Q(seller=user)).order_by('-timestamp')))

    user.refresh_from_db()
    return render(request, 'trading/trader.html', {
        'orders': orders,
        'trades': trades,
        'base_role': user.role,
        'capital': user.capital,
        'inventory': user.inventory,
    })


@login_required
def orderbook(request):
    base_user = _get_or_create_base_user(request.user)
    buy_orders = Order.objects.filter(
        is_matched=False, order_type='BUY', order_mode='LIMIT', price__isnull=False,
    ).order_by('-price')

    sell_orders = Order.objects.filter(
        is_matched=False, order_type='SELL', order_mode='LIMIT', price__isnull=False,
    ).order_by('price')

    trades = Trade.objects.all().order_by('-timestamp')

    return render(request, 'trading/orderbook.html', {
        'buy_orders': buy_orders,
        'sell_orders': sell_orders,
        'best_bid': buy_orders.first() if buy_orders else None,
        'best_ask': sell_orders.first() if sell_orders else None,
        'trades': trades,
        'base_role': base_user.role if base_user else None,
    })


@login_required
def modify(request):
    if not _is_admin(request.user):
        return redirect('role_router')
    base_user = _get_or_create_base_user(request.user)
    buy_orders = Order.objects.filter(
        is_matched=False, order_type='BUY', order_mode='LIMIT', price__isnull=False,
    ).order_by('-price')
    sell_orders = Order.objects.filter(
        is_matched=False, order_type='SELL', order_mode='LIMIT', price__isnull=False,
    ).order_by('price')

    trades = Trade.objects.all().order_by('-timestamp')

    return render(request, 'trading/modify.html', {
        'buy_orders': buy_orders,
        'sell_orders': sell_orders,
        'best_bid': buy_orders.first() if buy_orders else None,
        'best_ask': sell_orders.first() if sell_orders else None,
        'trades': trades,
        'base_role': base_user.role if base_user else None,
    })


@login_required
def modify_order_page(request):
    if not _is_admin(request.user):
        return redirect('role_router')
    base_user = _get_or_create_base_user(request.user)
    buy_orders = Order.objects.filter(
        is_matched=False, order_type='BUY', order_mode='LIMIT', price__isnull=False,
    ).order_by('-price')
    sell_orders = Order.objects.filter(
        is_matched=False, order_type='SELL', order_mode='LIMIT', price__isnull=False,
    ).order_by('price')

    trades = Trade.objects.all().order_by('-timestamp')

    return render(request, 'trading/modify_order.html', {
        'buy_orders': buy_orders,
        'sell_orders': sell_orders,
        'trades': trades,
        'base_role': base_user.role if base_user else None,
    })


def _apply_order_modification(order, new_quantity, new_disclosed, new_price):
    """Shared validation + write for order modification. Returns (ok, message)."""
    if order.is_matched:
        return False, 'Order has already been matched. No modifications allowed.'
    if new_quantity <= 0:
        return False, 'Quantity must be greater than 0.'
    if new_disclosed <= 0:
        return False, 'Disclosed quantity must be greater than 0.'
    if new_disclosed > new_quantity:
        return False, 'Cannot disclose more than the quantity.'
    if new_disclosed < new_quantity * 0.1:
        return False, 'Disclosed value must be at least 10% of quantity.'
    if order.price is not None and new_price <= 0:
        return False, 'Price must be greater than 0.'

    order.quantity = new_quantity
    order.disclosed = new_disclosed
    # original_quantity tracks the iceberg base; reset it so disclosed logic stays correct.
    order.original_quantity = new_quantity
    if order.price is not None:
        order.price = Decimal(str(new_price)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    order.save()
    broadcast_orderbook_update()
    return True, 'Order updated successfully.'


@login_required
def update_prev_order(request):
    if not _is_admin(request.user):
        return JsonResponse({'success': False, 'message': 'Admin access required.'}, status=403)
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            order_id = int(data.get('order_id'))
            new_quantity = int(data.get('quantity'))
            new_disclosed = int(data.get('disclosed_quantity'))
            new_price = float(data.get('price'))

            order = Order.objects.get(id=order_id)
            ok, message = _apply_order_modification(order, new_quantity, new_disclosed, new_price)
            return JsonResponse({'success': ok, 'message': message})

        except Order.DoesNotExist:
            return JsonResponse({'success': False, 'message': 'Order not found.'})
        except (ValueError, TypeError):
            return JsonResponse({'success': False, 'message': 'Invalid data provided.'})
        except Exception as e:
            return JsonResponse({'success': False, 'message': str(e)})


@login_required
def modify_order(request):
    """Participant-facing modification: a user can modify their OWN unmatched order."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Invalid request.'}, status=405)
    try:
        user = BaseUser.objects.get(username=request.user.username)
        data = json.loads(request.body)
        order_id = int(data.get('order_id'))
        new_quantity = int(data.get('quantity'))
        new_disclosed = int(data.get('disclosed_quantity'))
        raw_price = data.get('price')

        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id, user=user, is_matched=False)
            new_price = float(raw_price) if raw_price not in (None, '') else 0.0
            ok, message = _apply_order_modification(order, new_quantity, new_disclosed, new_price)
        return JsonResponse({'success': ok, 'message': message})

    except BaseUser.DoesNotExist:
        return JsonResponse({'success': False, 'message': 'User authentication failed'}, status=401)
    except Order.DoesNotExist:
        return JsonResponse({'success': False, 'message': 'Order not found, not yours, or already matched.'}, status=404)
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'message': 'Invalid request format'}, status=400)
    except (ValueError, TypeError):
        return JsonResponse({'success': False, 'message': 'Invalid data provided.'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=500)


@login_required
def clear_database(request):
    if not _is_admin(request.user):
        return redirect('role_router')
    Order.objects.all().delete()
    Trade.objects.all().delete()
    BaseUser.objects.filter(role='MARKET_MAKER').update(capital=MARKET_MAKER_INITIAL_CAPITAL, inventory=MARKET_MAKER_INITIAL_INVENTORY)
    BaseUser.objects.filter(role='TRADER').update(capital=TRADER_INITIAL_CAPITAL, inventory=TRADER_INITIAL_INVENTORY)
    return redirect('login')


def reset_everything(request):
    """Admin hard reset: wipe the order book, all trades, AND every participant
    account (traders + market makers). Admin accounts are preserved."""
    if not _is_admin(request.user):
        return redirect('role_router')
    Order.objects.all().delete()
    Trade.objects.all().delete()
    non_admin = BaseUser.objects.exclude(is_superuser=True).exclude(role='ADMIN')
    usernames = list(non_admin.values_list('username', flat=True))
    count = non_admin.count()
    non_admin.delete()  # FK CASCADE clears any remaining orders/trades for these users
    User.objects.filter(username__in=usernames).exclude(is_superuser=True).delete()
    messages.success(request, f'Full reset done: order book and trades cleared, and {count} participant account(s) removed. Admin accounts were preserved.')
    return redirect('admin_home')


@login_required
def get_buy_orders(request):
    if request.method == 'GET':
        buy_orders = Order.objects.filter(
            order_type='BUY', order_mode='LIMIT', price__isnull=False, is_matched=False,
        ).order_by('-price', 'timestamp')
        return JsonResponse({'buy_orders': [_serialize_order(order) for order in buy_orders]})
    return JsonResponse({'buy_orders': []}, status=405)


@login_required
def get_sell_orders(request):
    if request.method == 'GET':
        sell_orders = Order.objects.filter(
            order_type='SELL', order_mode='LIMIT', price__isnull=False, is_matched=False,
        ).order_by('price', 'timestamp')
        return JsonResponse({'sell_orders': [_serialize_order(order) for order in sell_orders]})
    return JsonResponse({'sell_orders': []}, status=405)


@login_required
def get_recent_trades(request):
    if request.method == 'GET':
        base_user = _get_or_create_base_user(request.user)
        if base_user and base_user.role == 'ADMIN':
            recent_trades = Trade.objects.all().order_by('-timestamp')[:10].values(
                'buyer__user_id', 'seller__user_id', 'price', 'quantity', 'timestamp'
            )
        else:
            recent_trades = Trade.objects.all().order_by('-timestamp')[:10].values(
                'price', 'quantity', 'timestamp'
            )
        return JsonResponse({'trades': list(recent_trades)})
    return JsonResponse({'trades': []}, status=405)


@login_required
def cancel_order(request):
    if request.method == 'POST':
        try:
            user = BaseUser.objects.get(username=request.user.username)
            data = json.loads(request.body)
            order_id = data.get('order_id')

            with transaction.atomic():
                order = Order.objects.get(id=order_id, user=user, is_matched=False)
                order.delete()

            broadcast_orderbook_update()
            return JsonResponse({'success': True, 'message': 'Order cancelled successfully'})

        except BaseUser.DoesNotExist:
            return JsonResponse({'success': False, 'message': 'User authentication failed'}, status=401)
        except Order.DoesNotExist:
            return JsonResponse({'success': False, 'message': 'Order not found or already matched'}, status=404)
        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'message': 'Invalid request format'}, status=400)
        except Exception as e:
            logger.error(f"Cancel order error: {str(e)}")
            return JsonResponse({'success': False, 'message': str(e)}, status=500)


# ============================================================
# BULK USER UPLOAD
# ============================================================

REQUIRED_HEADERS = ['Registration No', 'Name', 'Mail', 'Role', 'Password']
VALID_ROLES = {'TRADER', 'MARKET_MAKER'}


def _validate_csv_row(row_num, roll, username, mail, role, password):
    errors = []
    if not roll:
        errors.append('Registration No is empty.')
    elif not roll.isdigit():
        errors.append(f'Registration No "{roll}" must contain numbers only.')

    if not username:
        errors.append('Username is empty.')
    elif not re.match(r'^[a-zA-Z\s\-]+$', username):
        errors.append(f'Username "{username}" must contain alphabets and spaces only.')

    if not mail:
        errors.append('Mail is empty.')
    elif '@' not in mail or '.' not in mail:
        errors.append(f'Mail "{mail}" is not a valid email address.')

    if not role:
        errors.append('Role is empty.')
    elif role not in VALID_ROLES:
        errors.append(f'Role "{role}" must be exactly TRADER or MARKET_MAKER.')

    if not password:
        errors.append('Password is empty.')
    else:
        has_alpha = bool(re.search(r'[a-zA-Z]', password))
        has_digit = bool(re.search(r'\d', password))
        has_special = bool(re.search(r'[^a-zA-Z0-9]', password))
        if not (has_alpha and has_digit and has_special):
            errors.append('Password must contain a mix of alphabets, numbers, and special characters.')

    return errors


def register(request):
    if request.method == 'POST':
        form = UserRegisterForm(request.POST)
        if form.is_valid():
            user_id = form.cleaned_data['user_id']
            name = form.cleaned_data['name']
            email = form.cleaned_data['email']
            role = form.cleaned_data['role']
            password = form.cleaned_data['password1']

            if BaseUser.objects.filter(user_id=user_id).exists():
                form.add_error('user_id', 'A user with this ID already exists.')
                return render(request, 'trading/register.html', {'form': form})

            try:
                with transaction.atomic():
                    User.objects.create_user(
                        user_id=user_id, email=email, password=password, name=name, role=role,
                    )
                messages.success(request, 'Account created successfully. Please log in.')
                return redirect('login')
            except Exception as e:
                messages.error(request, f'Registration failed: {e}')
                return render(request, 'trading/register.html', {'form': form})
    else:
        form = UserRegisterForm()

    return render(request, 'trading/register.html', {'form': form})


@login_required
def export_positions_csv(request):
    """Admin: download the participant positions report (cash / inventory / P&L) as CSV."""
    if not _is_admin(request.user):
        return redirect('role_router')
    participants, mark = _participant_positions()
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="participant_positions.csv"'
    writer = csv.writer(response)
    writer.writerow(['Registration No', 'Name', 'Role', 'Cash', 'Inventory',
                     'Equity', 'P&L', 'Mark Price'])
    for p in participants:
        writer.writerow([
            p['user_id'], p['name'], p['role'],
            f"{p['capital']:.2f}", p['inventory'],
            f"{p['equity']:.2f}", f"{p['pnl']:.2f}",
            f"{mark:.2f}" if mark is not None else '',
        ])
    return response


def admin_account(request):
    """Admin: change own login Registration No and/or password."""
    if not _is_admin(request.user):
        return redirect('role_router')
    if request.method == 'POST':
        new_id = (request.POST.get('new_user_id') or '').strip()
        new_pw = request.POST.get('new_password') or ''
        confirm = request.POST.get('confirm_password') or ''
        user = request.user
        changed = []
        if new_id and new_id != user.user_id:
            clash = (BaseUser.objects.filter(user_id=new_id).exclude(pk=user.pk).exists()
                     or BaseUser.objects.filter(username=new_id).exclude(pk=user.pk).exists())
            if clash:
                messages.error(request, f'Registration No "{new_id}" is already taken.')
                return redirect('admin_account')
            user.user_id = new_id
            user.username = new_id
            changed.append('Registration No')
        if new_pw:
            if new_pw != confirm:
                messages.error(request, 'Passwords do not match.')
                return redirect('admin_account')
            if len(new_pw) < 6:
                messages.error(request, 'Password must be at least 6 characters.')
                return redirect('admin_account')
            user.set_password(new_pw)
            changed.append('password')
        if changed:
            user.save()
            if 'password' in changed:
                update_session_auth_hash(request, user)
            messages.success(request, 'Updated ' + ' and '.join(changed) + '.')
        else:
            messages.info(request, 'No changes were made.')
        return redirect('admin_account')
    return render(request, 'trading/account.html', {'base_role': 'ADMIN'})


def bulk_user_upload(request):
    if not _is_admin(request.user):
        return redirect('role_router')

    results = None

    if request.method == 'POST':
        csv_file = request.FILES.get('csv_file')
        if not csv_file:
            messages.error(request, 'No file uploaded.')
            return render(request, 'trading/bulk_upload.html', {'results': results})

        if not csv_file.name.endswith('.csv'):
            messages.error(request, 'Please upload a valid .csv file.')
            return render(request, 'trading/bulk_upload.html', {'results': results})

        try:
            decoded = csv_file.read().decode('utf-8-sig')
        except UnicodeDecodeError:
            messages.error(request, 'File encoding error. Please save your CSV as UTF-8.')
            return render(request, 'trading/bulk_upload.html', {'results': results})

        reader = csv.DictReader(io.StringIO(decoded))
        actual_headers = [h.strip() for h in reader.fieldnames if h.strip()] if reader.fieldnames else []

        if not all(header in actual_headers for header in REQUIRED_HEADERS):
            messages.error(request, f'Invalid headers. Missing required fields. Expected: {", ".join(REQUIRED_HEADERS)}')
            return render(request, 'trading/bulk_upload.html', {'results': results})

        created_users = []
        skipped_users = []
        invalid_rows = []

        for row_num, row in enumerate(reader, start=2):
            roll = (row.get('Registration No') or '').strip()
            name = (row.get('Name') or '').strip()
            mail = (row.get('Mail') or '').strip()
            role = (row.get('Role') or '').strip()
            password = (row.get('Password') or '').strip()

            row_errors = _validate_csv_row(row_num, roll, name, mail, role, password)
            if row_errors:
                invalid_rows.append({'row': row_num, 'Name': name or '—', 'errors': row_errors})
                continue

            if BaseUser.objects.filter(user_id=roll).exists():
                skipped_users.append({'row': row_num, 'Name': name, 'reason': f'Registration No {roll} already exists.'})
                continue

            try:
                with transaction.atomic():
                    User.objects.create_user(
                        user_id=roll, email=mail, password=password, name=name, role=role,
                    )
                created_users.append({'row': row_num, 'username': name, 'role': role, 'mail': mail})
            except Exception as e:
                invalid_rows.append({'row': row_num, 'username': name, 'errors': [str(e)]})

        results = {
            'created': created_users,
            'skipped': skipped_users,
            'invalid': invalid_rows,
            'total_created': len(created_users),
            'total_skipped': len(skipped_users),
            'total_invalid': len(invalid_rows),
        }

    return render(request, 'trading/bulk_upload.html', {'results': results})


# ============================================================
# BULK USER DELETE
# ============================================================

@login_required
def bulk_user_delete(request):
    if not _is_admin(request.user):
        return redirect('role_router')

    results = None

    if request.method == 'POST':
        csv_file = request.FILES.get('csv_file')
        if not csv_file:
            messages.error(request, 'No file uploaded.')
            return render(request, 'trading/bulk_delete.html', {'results': results})

        if not csv_file.name.endswith('.csv'):
            messages.error(request, 'Please upload a valid .csv file.')
            return render(request, 'trading/bulk_delete.html', {'results': results})

        try:
            decoded = csv_file.read().decode('utf-8-sig')
        except UnicodeDecodeError:
            messages.error(request, 'File encoding error. Please save your CSV as UTF-8.')
            return render(request, 'trading/bulk_delete.html', {'results': results})

        reader = csv.DictReader(io.StringIO(decoded))
        DELETE_HEADERS = ['Registration No', 'Name']
        if not reader.fieldnames or [h.strip() for h in reader.fieldnames[:2]] != DELETE_HEADERS:
            messages.error(request, f'Invalid headers. First two columns must be: {", ".join(DELETE_HEADERS)}')
            return render(request, 'trading/bulk_delete.html', {'results': results})

        deleted_users = []
        not_found = []
        error_rows = []

        for row_num, row in enumerate(reader, start=2):
            roll = (row.get('Registration No') or '').strip()
            name = (row.get('Name') or '').strip()
            display_name = name if name else roll

            if not roll:
                error_rows.append({'row': row_num, 'name': display_name or '—', 'reason': 'Registration No is empty.'})
                continue
            if not roll.isdigit():
                error_rows.append({'row': row_num, 'name': display_name, 'reason': f'Registration No "{roll}" must be numbers only.'})
                continue

            try:
                with transaction.atomic():
                    auth_exists = User.objects.filter(username=roll).exists()
                    base_exists = BaseUser.objects.filter(username=roll).exists()

                    if not auth_exists and not base_exists:
                        not_found.append({'row': row_num, 'name': display_name})
                        continue

                    Trader.objects.filter(username=roll).delete()
                    MarketMaker.objects.filter(username=roll).delete()
                    BaseUser.objects.filter(username=roll).delete()
                    User.objects.filter(username=roll).delete()

                    deleted_users.append({'row': row_num, 'name': display_name})
            except Exception as e:
                error_rows.append({'row': row_num, 'name': display_name, 'reason': str(e)})

        results = {
            'deleted': deleted_users,
            'not_found': not_found,
            'errors': error_rows,
            'total_deleted': len(deleted_users),
            'total_not_found': len(not_found),
            'total_errors': len(error_rows),
        }

    return render(request, 'trading/bulk_delete.html', {'results': results})
