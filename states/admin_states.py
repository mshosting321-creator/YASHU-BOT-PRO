from aiogram.fsm.state import State, StatesGroup


class AdminStates(StatesGroup):
    waiting_zip = State()
    add_stuff_name = State()
    add_stuff_price = State()
    add_stuff_desc = State()
    add_stuff_media = State()
    add_stuff_demo = State()
    add_stuff_showcase = State()
    add_stuff_proofs = State()
    add_lessons_media = State()
    rename_stuff = State()
    price_stuff = State()
    set_showcase = State()
    add_proofs = State()
    set_start_text = State()
    set_start_photo = State()
    set_start_video = State()
    add_admin_id = State()
    waiting_log_group = State()
    add_name = State()
    choose_kind = State()
    add_detail = State()
    add_note = State()
    add_qr = State()
    add_stars_username = State()
    user_lookup = State()
    balance_op = State()
    broadcast_text = State()
    clone_pending = State()


class PaymentStates(StatesGroup):
    waiting_screenshot = State()


class UserStates(StatesGroup):
    bselect = State()
