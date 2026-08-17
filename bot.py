import os
import logging
import secrets
import asyncio
import requests
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.getenv('BOT_TOKEN')
SB_URL = os.getenv('SUPABASE_URL')
SB_KEY = os.getenv('SUPABASE_KEY')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))

logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
H = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}

def sb_select(table, **f):
    params = {}
    for c, v in f.items():
        if v is True: v = 'true'
        elif v is False: v = 'false'
        params[c] = f"eq.{v}"
    return requests.get(f"{SB_URL}/rest/v1/{table}", headers=H, params=params).json()

def sb_insert(table, payload):
    return requests.post(f"{SB_URL}/rest/v1/{table}", headers={**H, "Prefer": "return=representation"}, json=payload).json()

def sb_update(table, rid, payload):
    return requests.patch(f"{SB_URL}/rest/v1/{table}?id=eq.{rid}", headers=H, json=payload).status_code

def bot_row(bid):
    r = sb_select('bots', id=bid)
    return r[0] if r else None

def is_active_sub(bid, uid):
    r = sb_select('subscribers', bot_id=bid, telegram_id=uid, status='active')
    if not r: return False
    exp = r[0].get('data_expiracao')
    return not (exp and datetime.fromisoformat(exp) < datetime.now())

# ---------- BOT DA CRIADORA (clientes) ----------
def make_start(bid):
    async def h(update, ctx):
        b = bot_row(bid); uid = update.effective_user.id
        if is_active_sub(bid, uid):
            await update.message.reply_text("🔥 *VIP ativo!* Aproveite 😈", parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📸 Ver conteúdo", callback_data='ver')]]))
        else:
            await update.message.reply_text(
                f"🔞 *{b['nome_exibicao']}*\n\nAssinatura: R$ {b['preco']}/mês\n\n*Pix:* `{b['chave_pix']}`\n\nApós pagar, o acesso libera em até 1h ⏳",
                parse_mode='Markdown')
    return h

def make_cb(bid):
    async def h(update, ctx):
        q = update.callback_query
        await q.answer()
        uid = update.effective_user.id
        if not is_active_sub(bid, uid):
            return await q.message.reply_text("⚠️ Assinatura inativa.")
        medias = sb_select('medias', bot_id=bid)
        if not medias:
            return await q.message.reply_text("📸 Conteúdo em breve!")
        for m in medias:
            if m['file_type'] == 'photo':
                await ctx.bot.send_photo(uid, m['file_id'], caption=m['legenda'])
            else:
                await ctx.bot.send_video(uid, m['file_id'], caption=m['legenda'])
    return h

# ---------- CRIADORA PUBLICANDO ----------
def make_media(bid):
    async def h(update, ctx):
        b = bot_row(bid); uid = update.effective_user.id
        if not b or not b.get('creator_id'): return
        cs = sb_select('creators', id=b['creator_id'])
        if not cs or cs[0].get('telegram_id') != uid: return
        msg = update.message
        ftype = 'photo' if msg.photo else 'video'
        fid = msg.photo[-1].file_id if msg.photo else msg.video.file_id
        sb_insert('medias', {'bot_id': bid, 'file_id': fid, 'file_type': ftype, 'legenda': msg.caption or ''})
        subs = sb_select('subscribers', bot_id=bid, status='active')
        for s in subs:
            try:
                if ftype == 'photo':
                    await ctx.bot.send_photo(s['telegram_id'], fid, caption=msg.caption)
                else:
                    await ctx.bot.send_video(s['telegram_id'], fid, caption=msg.caption)
            except Exception as e:
                logger.warning(e)
        await msg.reply_text(f"✅ Publicado para {len(subs)} assinante(s)!")
    return h

def make_stats(bid):
    async def h(update, ctx):
        subs = sb_select('subscribers', bot_id=bid, status='active')
        await update.message.reply_text(f"📊 Assinantes ativos: {len(subs)}")
    return h

# ---------- HUB (VOCÊ, ADMIN) ----------
async def hub_start(update, ctx):
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("⚒️ PauloForge Soluções — plataforma de bots.")
    await update.message.reply_text("🛠️ *HUB ADMIN*\n\n/novabot TOKEN | Nome | Pix\n/ativar bot_id user_id [dias]\n/gerartoken email\n/link creator_id telegram_id\n/bots", parse_mode='Markdown')

async def hub_novabot(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    try:
        p = update.message.text.split('|')
        token = p[0].replace('/novabot', '', 1).strip(); nome = p[1].strip(); pix = p[2].strip()
    except Exception:
        return await update.message.reply_text("Formato: /novabot TOKEN | Nome | Pix")
    rows = sb_insert('bots', {'bot_token': token, 'nome_exibicao': nome, 'chave_pix': pix})
    if not rows:
        return await update.message.reply_text("❌ Falha ao cadastrar.")
    await start_bot_app(rows[0])
    await update.message.reply_text(f"✅ Bot *{nome}* (id {rows[0]['id']}) NO AR!", parse_mode='Markdown')

async def hub_ativar(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    p = update.message.text.split()
    bid, uid = int(p[1]), int(p[2])
    dias = int(p[3]) if len(p) > 3 else 30
    exp = (datetime.now() + timedelta(days=dias)).isoformat()
    ex = sb_select('subscribers', bot_id=bid, telegram_id=uid)
    if ex:
        sb_update('subscribers', ex[0]['id'], {'status': 'active', 'data_expiracao': exp})
    else:
        sb_insert('subscribers', {'bot_id': bid, 'telegram_id': uid, 'status': 'active', 'data_expiracao': exp})
    await update.message.reply_text(f"✅ Assinante {uid} ativado no bot {bid} ({dias} dias).")

async def hub_gerartoken(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    email = update.message.text.split(maxsplit=1)[1].strip()
    cs = sb_select('creators', email=email)
    cid = cs[0]['id'] if cs else sb_insert('creators', {'email': email})[0]['id']
    tok = secrets.token_urlsafe(16)
    sb_insert('access_tokens', {'creator_id': cid, 'token': tok})
    await update.message.reply_text(f"🔑 Token do painel pra {email}:\n`{tok}`", parse_mode='Markdown')

async def hub_link(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    p = update.message.text.split()
    sb_update('creators', int(p[1]), {'telegram_id': int(p[2])})
    await update.message.reply_text("🔗 Criadora vinculada ao Telegram.")

async def hub_bots(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    bots = sb_select('bots')
    await update.message.reply_text("\n".join(f"#{b['id']} {b['nome_exibicao']}" for b in bots) or "Sem bots.")

# ---------- ENVIA CÓDIGO DO PAINEL NO TELEGRAM ----------
async def envia_codigos(app):
    while True:
        try:
            for lc in sb_select('login_codes', enviado=False):
                cs = sb_select('creators', id=lc['creator_id'])
                if cs and cs[0].get('telegram_id'):
                    await app.bot.send_message(cs[0]['telegram_id'], f"🔐 Código de acesso ao painel: {lc['code']}")
                    sb_update('login_codes', lc['id'], {'enviado': True})
        except Exception as e:
            logger.warning(e)
        await asyncio.sleep(5)

# ---------- MOTOR MULTI-BOT ----------
running = {}

async def start_bot_app(b):
    app = Application.builder().token(b['bot_token']).build()
    bid = b['id']
    app.add_handler(CommandHandler('start', make_start(bid)))
    app.add_handler(CallbackQueryHandler(make_cb(bid)))
    app.add_handler(MessageHandler((filters.PHOTO | filters.VIDEO), make_media(bid)))
    app.add_handler(CommandHandler('stats', make_stats(bid)))
    await app.initialize(); await app.start(); await app.updater.start_polling()
    running[bid] = app
    logger.info(f"🤖 Bot #{bid} {b['nome_exibicao']} online")

async def post_init(application):
    asyncio.create_task(envia_codigos(application))
    for b in sb_select('bots', ativo=True):
        try:
            await start_bot_app(b)
        except Exception as e:
            logger.error(f"erro bot {b['id']}: {e}")

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler('start', hub_start))
    app.add_handler(CommandHandler('novabot', hub_novabot))
    app.add_handler(CommandHandler('ativar', hub_ativar))
    app.add_handler(CommandHandler('gerartoken', hub_gerartoken))
    app.add_handler(CommandHandler('link', hub_link))
    app.add_handler(CommandHandler('bots', hub_bots))
    logger.info("🛠️ HUB PauloForge online!")
    app.run_polling()

if __name__ == '__main__':
    main()
