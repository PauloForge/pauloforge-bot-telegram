import os, re, logging, secrets, asyncio, requests
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.getenv('BOT_TOKEN')
SB_URL = os.getenv('SUPABASE_URL')
SB_KEY = os.getenv('SUPABASE_KEY')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
PAINEL_URL = os.getenv('PAINEL_URL', '')

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

def first(rows):
    return rows[0] if isinstance(rows, list) and rows else None

def ins_or_raise(table, payload, passo):
    r = sb_insert(table, payload)
    row = first(r)
    if not row:
        msg = r.get('message') if isinstance(r, dict) else 'resposta vazia'
        raise Exception(f"{passo}: {msg}")
    return row

def bot_row(bid):
    return first(sb_select('bots', id=bid))

def is_active_sub(bid, uid):
    r = first(sb_select('subscribers', bot_id=bid, telegram_id=uid, status='active'))
    if not r: return False
    exp = r.get('data_expiracao')
    return not (exp and datetime.fromisoformat(exp) < datetime.now())

# ---------- BOTS DAS CRIADORAS ----------
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
        q = update.callback_query; await q.answer(); uid = update.effective_user.id
        if not is_active_sub(bid, uid):
            return await q.message.reply_text("⚠️ Assinatura inativa.")
        medias = sb_select('medias', bot_id=bid)
        if not medias:
            return await q.message.reply_text("📸 Conteúdo em breve!")
        for m in medias:
            if m['file_type'] == 'photo': await ctx.bot.send_photo(uid, m['file_id'], caption=m['legenda'])
            else: await ctx.bot.send_video(uid, m['file_id'], caption=m['legenda'])
    return h

def make_media(bid):
    async def h(update, ctx):
        b = bot_row(bid); uid = update.effective_user.id
        if not b or not b.get('creator_id'): return
        cs = first(sb_select('creators', id=b['creator_id']))
        if not cs or cs.get('telegram_id') != uid: return
        msg = update.message
        ftype = 'photo' if msg.photo else 'video'
        fid = msg.photo[-1].file_id if msg.photo else msg.video.file_id
        sb_insert('medias', {'bot_id': bid, 'file_id': fid, 'file_type': ftype, 'legenda': msg.caption or ''})
        subs = sb_select('subscribers', bot_id=bid, status='active')
        for s in subs:
            try:
                if ftype == 'photo': await ctx.bot.send_photo(s['telegram_id'], fid, caption=msg.caption)
                else: await ctx.bot.send_video(s['telegram_id'], fid, caption=msg.caption)
            except Exception as e: logger.warning(e)
        await msg.reply_text(f"✅ Publicado para {len(subs)} assinante(s)!")
    return h

def make_stats(bid):
    async def h(update, ctx):
        subs = sb_select('subscribers', bot_id=bid, status='active')
        await update.message.reply_text(f"📊 Assinantes ativos: {len(subs)}")
    return h

# ---------- WIZARD CRIAR BOT ----------
wizard = {}

async def iniciar_wizard(update, ctx, uid):
    wizard[uid] = {'step': 'token', 'dados': {}, 'msgs': []}
    m = await update.message.reply_text("🤖 *VAMOS CRIAR SEU BOT!*\n\n*Passo 1/4* — Abra o @BotFather, crie um bot e cole aqui o TOKEN dele:", parse_mode='Markdown')
    wizard[uid]['msgs'].append(m.message_id)

async def finalizar(update, ctx, uid):
    w = wizard.pop(uid); d = w['dados']
    wait = await update.message.reply_text("⏳ Aguarde, seu acesso está sendo gerado...")
    try:
        email = d['email'].lower().strip()
        c = first(sb_select('creators', email=email))
        if c:
            cid = c['id']
            sb_update('creators', cid, {'telegram_id': uid, 'nome': d['nome']})
        else:
            cid = ins_or_raise('creators', {'email': email, 'telegram_id': uid, 'nome': d['nome']}, 'criar conta')['id']
        brow = first(sb_select('bots', bot_token=d['token']))
        if brow:
            sb_update('bots', brow['id'], {'creator_id': cid, 'nome_exibicao': d['nome'], 'chave_pix': d['pix'], 'ativo': True})
        else:
            brow = ins_or_raise('bots', {'bot_token': d['token'], 'bot_username': d['username'], 'nome_exibicao': d['nome'], 'chave_pix': d['pix'], 'creator_id': cid}, 'cadastrar bot')
        sb_insert('access_tokens', {'creator_id': cid, 'token': secrets.token_urlsafe(12)})
        tok_painel = first(sb_select('access_tokens', creator_id=cid))
        if brow:
            try: await start_bot_app(brow)
            except Exception as e: logger.error(e)
        for mid in w['msgs']:
            try: await ctx.bot.delete_message(uid, mid)
            except Exception: pass
        try: await wait.delete()
        except Exception: pass
        await ctx.bot.send_message(uid,
            f"🎉 PRONTO, {d['nome']}!\n\n🤖 Seu bot: @{d['username']} (id {brow['id']})\n🔑 Token do painel: {tok_painel['token'] if tok_painel else '-'}\n📧 Email: {email}\n🌐 Painel: {PAINEL_URL or 'em breve'}\n\n📖 Mande fotos/vídeos DIRETO no seu bot pra publicar pros assinantes!\n/stats no seu bot mostra seus números.")
    except Exception as e:
        logger.exception(e)
        try: await update.message.reply_text(f"❌ Erro ao gerar acesso: {e}")
        except Exception: pass

async def wizard_text(update, ctx, uid):
    w = wizard[uid]; txt = update.message.text.strip()
    if w['step'] == 'token':
        if not re.match(r'^\d+:[A-Za-z0-9_-]{20,}$', txt):
            return await update.message.reply_text("❌ Token inválido. Cole o token completo do BotFather.")
        r = requests.get(f"https://api.telegram.org/bot{txt}/getMe").json()
        if not r.get('ok'):
            return await update.message.reply_text("❌ O Telegram recusou esse token.")
        w['dados']['token'] = txt; w['dados']['username'] = r['result']['username']
        w['step'] = 'email'
        await update.message.reply_text("✅ Token válido!\n\n*Passo 2/4* — Seu melhor e-mail:", parse_mode='Markdown')
    elif w['step'] == 'email':
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', txt):
            return await update.message.reply_text("❌ E-mail inválido.")
        w['dados']['email'] = txt; w['step'] = 'nome'
        await update.message.reply_text("*Passo 3/4* — Nome de exibição (como os clientes vão te ver):", parse_mode='Markdown')
    elif w['step'] == 'nome':
        w['dados']['nome'] = txt; w['step'] = 'pix'
        await update.message.reply_text("*Passo 4/4* — Sua chave Pix pra receber:", parse_mode='Markdown')
    elif w['step'] == 'pix':
        w['dados']['pix'] = txt
        await finalizar(update, ctx, uid)

# ---------- WIZARD ATIVAR (admin) ----------
ativ = {}

async def iniciar_ativar(update, ctx, uid):
    bots = sb_select('bots', ativo=True)
    if not bots:
        return await update.message.reply_text("❌ Nenhum bot cadastrado ainda.")
    ativ[uid] = {'step': 'user', 'bots': {str(b['id']): b['id'] for b in bots}}
    lista = "\n".join(f"#{b['id']} — {b['nome_exibicao']}" for b in bots)
    await update.message.reply_text(f"🎯 *ATIVAR ASSINANTE*\n\nBots:\n{lista}\n\nDigite o NÚMERO do bot:", parse_mode='Markdown')

async def ativ_text(update, ctx, uid):
    a = ativ[uid]; txt = update.message.text.strip()
    if a['step'] == 'user':
        bid = a['bots'].get(txt.replace('#', ''))
        if not bid:
            return await update.message.reply_text("❌ Número de bot inválido.")
        a['bid'] = bid; a['step'] = 'dias'
        await update.message.reply_text("Agora o ID do cliente (número do Telegram dele):")
    elif a['step'] == 'dias':
        if not txt.isdigit():
            return await update.message.reply_text("❌ Só números.")
        a['uidc'] = int(txt); a['step'] = 'confirma'
        await update.message.reply_text("Por quantos dias? (ex: 30)")
    elif a['step'] == 'confirma':
        dias = int(txt) if txt.isdigit() else 30
        exp = (datetime.now() + timedelta(days=dias)).isoformat()
        ex = first(sb_select('subscribers', bot_id=a['bid'], telegram_id=a['uidc']))
        if ex: sb_update('subscribers', ex['id'], {'status': 'active', 'data_expiracao': exp})
        else: sb_insert('subscribers', {'bot_id': a['bid'], 'telegram_id': a['uidc'], 'status': 'active', 'data_expiracao': exp})
        ativ.pop(uid)
        await update.message.reply_text(f"✅ Assinante {a['uidc']} ativado no bot #{a['bid']} por {dias} dias!")

# ---------- HUB ----------
async def hub_start(update, ctx):
    uid = update.effective_user.id
    if uid == ADMIN_ID:
        return await update.message.reply_text("🛠️ *HUB ADMIN*\n\n/criarbot — wizard novo bot\n/ativar — wizard ativar assinante\n/gerartoken email\n/bots", parse_mode='Markdown')
    await update.message.reply_text("⚒️ *PauloForge Soluções*\nTenha seu próprio bot +18 no Telegram.", parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🤖 Criar meu bot", callback_data='criarbot')]]))

async def hub_cb(update, ctx):
    q = update.callback_query; await q.answer()
    if q.data == 'criarbot':
        uid = q.from_user.id
        wizard[uid] = {'step': 'token', 'dados': {}, 'msgs': []}
        m = await q.message.reply_text("🤖 *VAMOS CRIAR SEU BOT!*\n\n*Passo 1/4* — Abra o @BotFather, crie um bot e cole aqui o TOKEN dele:", parse_mode='Markdown')
        wizard[uid]['msgs'].append(m.message_id)

async def hub_criarbot(update, ctx):
    await iniciar_wizard(update, ctx, update.effective_user.id)

async def hub_ativar(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    await iniciar_ativar(update, ctx, update.effective_user.id)

async def hub_gerartoken(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    email = update.message.text.split(maxsplit=1)[1].strip().lower()
    c = first(sb_select('creators', email=email))
    cid = c['id'] if c else ins_or_raise('creators', {'email': email}, 'criar conta')['id']
    tok = secrets.token_urlsafe(16)
    sb_insert('access_tokens', {'creator_id': cid, 'token': tok})
    await update.message.reply_text(f"🔑 Token do painel pra {email}:\n{tok}")

async def hub_bots(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    bots = sb_select('bots')
    await update.message.reply_text("\n".join(f"#{b['id']} {b['nome_exibicao']} @{b.get('bot_username') or '-'}" for b in bots) or "Sem bots.")

# ---------- CÓDIGOS DO PAINEL ----------
async def envia_codigos(app):
    while True:
        try:
            for lc in sb_select('login_codes', enviado=False):
                cs = first(sb_select('creators', id=lc['creator_id']))
                if cs and cs.get('telegram_id'):
                    await app.bot.send_message(cs['telegram_id'], f"🔐 Código de acesso ao painel: {lc['code']}")
                    sb_update('login_codes', lc['id'], {'enviado': True})
        except Exception as e: logger.warning(e)
        await asyncio.sleep(5)

# ---------- MOTOR MULTI-BOT ----------
running = {}
async def start_bot_app(b):
    if b['id'] in running: return
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
        try: await start_bot_app(b)
        except Exception as e: logger.error(f"erro bot {b['id']}: {e}")

async def on_text(update, ctx):
    uid = update.effective_user.id
    if uid in wizard: return await wizard_text(update, ctx, uid)
    if uid in ativ and uid == ADMIN_ID: return await ativ_text(update, ctx, uid)

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler('start', hub_start))
    app.add_handler(CommandHandler('criarbot', hub_criarbot))
    app.add_handler(CommandHandler('novabot', hub_criarbot))
    app.add_handler(CommandHandler('ativar', hub_ativar))
    app.add_handler(CommandHandler('gerartoken', hub_gerartoken))
    app.add_handler(CommandHandler('bots', hub_bots))
    app.add_handler(CallbackQueryHandler(hub_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    logger.info("🛠️ HUB PauloForge v3.2 online!")
    app.run_polling()

if __name__ == '__main__':
    main()
