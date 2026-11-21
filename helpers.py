async def send_originals_to_client(bot: Bot, order_id: str) -> bool:
    """Send clean originals only after the owner explicitly confirms payment."""
    async with Session() as db:
        result = await db.execute(
            update(Order).where(Order.id == order_id, Order.status == "payment_review")
            .values(status="sending_originals")
        )
        if result.rowcount != 1:
            return False
        await db.commit()
        order = await db.get(Order, order_id)
        brief = dict(order.brief or {})
        client = await db.get(Client, order.client_id)
        client_id = client.telegram_id if client else None
        assets = sorted(list(brief.get("design_assets", [])),
                        key=lambda item: (int(item.get("slide", 0)), int(item.get("variant", 0))))
    originals = [Path(item.get("final_path", "")) for item in assets
                 if Path(item.get("final_path", "")).is_file()]
    if not client_id or not originals or len(originals) != len(assets):
        await _set_order_status(order_id, "payment_review")
        return False
    try:
        await bot.send_message(client_id, f"Оплата по заявке №{order_id[:8]} подтверждена. Отправляю оригиналы без водяного знака.")
        await _send_album(bot, client_id, originals, caption=f"Оригиналы по заявке {order_id[:8]}", documents=True)
    except Exception:
        await _set_order_status(order_id, "payment_review")
        return False
    async with Session() as db:
        order = await db.get(Order, order_id)
        if order:
            brief = dict(order.brief or {})
            brief["payment"] = {"status": "owner_confirmed", "confirmed_at": now().isoformat()}
            order.brief = brief
            order.status = "completed"
            await db.commit()
    return True


@router.callback_query(F.data.startswith("payment:confirm:"))
async def confirm_payment_callback(call: CallbackQuery):
    if call.from_user.id != settings.admin_id:
        await call.answer("Только администратор может подтверждать оплату.", show_alert=True)
        return
    order_id = call.data.split(":", 2)[2]
    if await send_originals_to_client(call.bot, order_id):
        await call.answer("Оплата отмечена, оригиналы отправлены.")
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.answer(f"Оригиналы по заявке {order_id[:8]} отправлены клиенту.")
    else:
        await call.answer("Заявка не готова, файлы отсутствуют или отправка не удалась.", show_alert=True)


@router.callback_query(F.data.startswith("feedback:"))
async def process_client_feedback(call: CallbackQuery):
    try:
        _, action, order_id = call.data.split(":", 2)
    except (AttributeError, ValueError):
        await call.answer("Некорректное действие.", show_alert=True)
        return
    async with Session() as db:
        order = await db.get(Order, order_id)
        if order is None:
            await call.answer("Заявка не найдена.", show_alert=True)
            return
        client = await db.get(Client, order.client_id)
        if client is None or client.telegram_id != call.from_user.id:
            await call.answer("Это не ваша заявка.", show_alert=True)
            return
        if order.status != "demo_sent" or action not in {"like", "revise", "paid"}:
            await call.answer("Действие недоступно для этой заявки.", show_alert=True)
            return
        brief = dict(order.brief or {})
        brief.setdefault("feedback", {})[action] = now().isoformat()
        order.brief = brief
        if action == "revise":
            order.status = "waiting_revision"
        elif action == "paid":
            order.status = "payment_review"
        await db.commit()
    if action == "like":
        await call.answer("Спасибо! Администратор увидит ваш отзыв.")
        await call.bot.send_message(settings.admin_id, f"Клиент одобрил демо по заявке {order_id[:8]}.")
    elif action == "revise":
        await call.answer("Напишите одним сообщением, какие правки нужны.")
        await call.message.answer("Опишите, что изменить в демо. Ваши замечания будут переданы администратору.")
        await call.bot.send_message(settings.admin_id, f"Клиент запросил правки по заявке {order_id[:8]}.")
    else:
        await call.answer("Запрос на подтверждение оплаты передан администратору.")
        await call.message.answer("Спасибо. Администратор проверит поступление платежа. Оригиналы будут отправлены после подтверждения.")
        await call.bot.send_message(
            settings.admin_id,
            f"Клиент сообщил об оплате по заявке {order_id[:8]}. Проверь поступление средств и подтверди вручную.",
            reply_markup=payment_review_menu(order_id),
        )
