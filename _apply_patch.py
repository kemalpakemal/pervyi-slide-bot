from pathlib import Path
p = Path("bot.py")
s = p.read_text(encoding="utf-8")
s = s.replace(
'''def design_review_menu(order_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отправить демо клиенту", callback_data=f"design:send:{order_id}")]
    ])
''',
'''def design_review_menu(order_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отправить демо клиенту", callback_data=f"design:send:{order_id}")]
    ])


def payment_review_menu(order_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подтвердить оплату и выдать оригиналы", callback_data=f"payment:confirm:{order_id}")]
    ])
''')
helpers = Path("helpers.py").read_text(encoding="utf-8")
anchor='@router.message(AdminAction.manual_images, F.photo | F.document)'
assert anchor in s
s=s.replace(anchor, helpers+"\n\n"+anchor)
old = '''        await call.bot.send_message(
            client_telegram_id,
            f"Подготовлено демо по заявке №{order_id[:8]}. Это предварительные изображения с водяным знаком. "
            "Напиши, что нужно поправить. Оригиналы без водяного знака передаются только после согласования и оплаты."
        )
'''
new = '''        await call.bot.send_message(
            client_telegram_id,
            f"Подготовлено демо по заявке №{order_id[:8]}. Это предварительные изображения с водяным знаком. "
            "Оцените результат: можно подтвердить, запросить правки или сообщить об оплате. "
            "Оригиналы без водяного знака отправляются только после ручной проверки оплаты администратором.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Понравилось", callback_data=f"feedback:like:{order_id}")],
                [InlineKeyboardButton(text="Нужны правки", callback_data=f"feedback:revise:{order_id}")],
                [InlineKeyboardButton(text="Я оплатил", callback_data=f"feedback:paid:{order_id}")],
            ]),
        )
'''
assert old in s
s=s.replace(old,new)
oldpaid='''@router.message(Command("paid"))
async def paid(message: Message):
    if message.from_user.id == settings.admin_id:
        await message.answer("Подтверждение оплаты будет добавлено на этапе 4. Сейчас оплата не подтверждается и оригиналы не выдаются.")
'''
newpaid='''@router.message(Command("paid"))
async def paid(message: Message):
    if message.from_user.id != settings.admin_id:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or len(parts[1].strip()) < 8:
        await message.answer("Укажи ID заявки: /paid <ID>. Используй команду только после проверки поступления платежа.")
        return
    prefix = parts[1].strip()
    async with Session() as db:
        matches = list((await db.scalars(
            select(Order).where(Order.id.like(f"{prefix}%")).limit(2)
        )).all())
        if len(matches) != 1 or matches[0].status != "payment_review":
            await message.answer("Заявка не найдена, ID неоднозначен или клиент ещё не сообщил об оплате.")
            return
        order_id = matches[0].id
    if await send_originals_to_client(message.bot, order_id):
        await message.answer(f"Оригиналы по заявке {order_id[:8]} отправлены.")
    else:
        await message.answer("Не удалось отправить оригиналы. Проверь файлы и доступность клиента.")
'''
assert oldpaid in s
s=s.replace(oldpaid,newpaid)
s=s.replace(
    '.where(Order.client_id == client.id, Order.status == "waiting_client_info")',
    '.where(Order.client_id == client.id, Order.status.in_(["waiting_client_info", "waiting_revision"]))'
)
oldblock='''        order_id = order.id
        previous = order.assistant_notes or ""
        order.assistant_notes = previous + f"\\nОтвет клиента: {message.text[:3000]}"
        order.status = "admin_review"
'''
newblock='''        order_id = order.id
        is_revision = order.status == "waiting_revision"
        if is_revision:
            brief = dict(order.brief or {})
            brief["style"] = str(brief.get("style") or "") + "\\nПравки клиента: " + message.text[:2000]
            order.brief = brief
        previous = order.assistant_notes or ""
        order.assistant_notes = previous + f"\\nОтвет клиента: {message.text[:3000]}"
        order.status = "accepted" if is_revision else "admin_review"
'''
assert oldblock in s
s=s.replace(oldblock,newblock)
oldreply='''    await message.answer(f"Ответ по заявке №{order_id[:8]} передан администратору.")
    await send_order_card(message.bot, settings.admin_id, order_id)
'''
newreply='''    await message.answer(f"Ответ по заявке №{order_id[:8]} передан администратору.")
    if is_revision:
        await message.bot.send_message(
            settings.admin_id,
            f"Клиент прислал правки по заявке {order_id[:8]}. Проверь их и запусти повторную генерацию через /generate {order_id[:8]}.",
        )
    await send_order_card(message.bot, settings.admin_id, order_id)
'''
assert oldreply in s
s=s.replace(oldreply,newreply)
p.write_text(s,encoding="utf-8")
