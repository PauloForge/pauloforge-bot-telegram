import os, re, logging, secrets, asyncio, requests
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.getenv('BOT_TOKEN')
SB_URL = os.getenv('SUPABASE_URL')
SB_KEY = os.getenv('SUPABASE_KEY')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
PAINEL_URL = os.getenv('PAINEL_URL', '')
WA = 'https://wa.me/5567993030021'

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

def bot_row(bid):
    return first(sb_select('bots', id=bid))

def is_active_sub(bid, uid):
    r = first(sb_select('subscribers', bot_id=bid, telegram_id=uid, status='active'))
    if not r: return False
    exp = r.get('data_expiracao')
    return not (exp and datetime.fromisoformat(exp) < datetime.now())

def btn_assinar(b):
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        f"💳 Assinar por R$ {b['preco']}/mês",
        url=f"{WA}?text=Quero%20assinar%20{b['nome_exibicao']}")]])

# ---------- BOTS DAS CRIADORAS ----------
def make_start(bid):
    async def h(update, ctx):
        b = bot_row(bid); uid = update.effective_user.id
        if is_active_sub(bid, uid):
            await update.message.reply_text("🔥 *VIP ativo!* Aproveite 😈", parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📸 Ver conteúdo", callback_data='ver')]]))
            return
        if not first(sb_select('leads', bot_id=bid, telegram_id=uid)):
            sb_insert('leads', {'bot_id': bid, 'telegram_id': uid})
        legenda = f"🔞 *{b['nome_exibicao']}*\n\nAssinatura: R$ {b['preco']}/mês\n*Pix:* `{b['chave_pix']}`\n\nAssine e receba TUDO na hora 😈"
        if b.get('welcome_video'):
            try:
                await ctx.bot.send_video(uid, b['welcome_video'], caption=legenda, parse_mode='Markdown', reply_markup=btn_assinar(b))
                return
            except Exception as e: logger.warning(e)
        await update.message.reply_text(legenda + "\n\nToque no botão pra assinar 👇", parse_mode='Markdown', reply_markup=btn_assinar(b))
    return h

def make_cb(bid):
    async def h(update, ctx):
        q = update.callback_query; await q.answer(); uid = update.effective_user.id
        if not is_active_sub(bid, uid):
            return await q.message.reply_text("⚠️ Só assinantes. Toque abaixo 👇", reply_markup=btn_assinar(bot_row(bid)))
        medias = sb_select('medias', bot_id=bid, tipo='vip')
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
        sb_insert('medias', {'bot_id': bid, 'file_id': fid, 'file_type': ftype, 'legenda': msg.caption or '', 'tipo': 'vip'})
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
        leads = sb_select('leads', bot_id=bid)
        await update.message.reply_text(f"📊 Assinantes: {len(subs)}\n👀 Curiosos: {len(leads)}")
    return h

def make_vincular(bid):
    async def h(update, ctx):
        b = bot_row(bid)
        if not b or not b.get('creator_id'): return
        code = update.message.text.split(maxsplit=1)[1].strip() if ' ' in update.message.text else ''
        cs = first(sb_select('creators', id=b['creator_id'], link_code=code))
        if not cs:
            return await update.message.reply_text("❌ Código inválido.")
        sb_update('creators', cs['id'], {'telegram_id': update.effective_user.id})
        await update.message.reply_text("🔗 Telegram vinculado ao painel!")
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
            cid = c['id']; sb_update('creators', cid, {'telegram_id': uid, 'nome': d['nome']})
        else:
            cid = first(sb_insert('creators', {'email': email, 'telegram_id': uid, 'nome': d['nome'], 'link_code': str(secrets.randbelow(9000) + 1000)}))['id']
            c2 = first(sb_select('creators', id=cid))
            if c2 and not c2.get('ref_code'): sb_update('creators', cid, {'ref_code': 'PF' + str(cid)})
        brow = first(sb_select('bots', bot_token=d['token']))
        if brow:
            sb_update('bots', brow['id'], {'creator_id': cid, 'nome_exibicao': d['nome'], 'chave_pix': d['pix'], 'ativo': True, 'expira_em': (datetime.now() + timedelta(days=1)).isoformat()})
        else:
            brow = first(sb_insert('bots', {'bot_token': d['token'], 'bot_username': d['username'], 'nome_exibicao': d['nome'], 'chave_pix': d['pix'], 'creator_id': cid, 'expira_em': (datetime.now() + timedelta(days=1)).isoformat()}))
        tok = secrets.token_urlsafe(12)
        sb_insert('access_tokens', {'creator_id': cid, 'token': tok})
        for mid in w['msgs']:
            try: await ctx.bot.delete_message(uid, mid)
            except Exception: pass
        try: await wait.delete()
        except Exception: pass
        await ctx.bot.send_message(uid,
            f"🎉 PRONTO, {d['nome']}!\n\n🤖 Seu bot: @{d['username']} (id {brow['id']})\n🔑 Token do painel: {tok}\n📧 Email: {email}\n🌐 Painel: {PAINEL_URL or 'em breve'}\n🎁 Teste grátis: 1 dia.\n\n📖 O worker liga seu bot em até 20s. Mande fotos/vídeos nele pra publicar!")
    except Exception as e:
        logger.exception(e)
        try: await update.message.reply_text(f"❌ Erro ao gerar acesso: {e}")
        except Exception: pass

async def wizard_text(update, ctx, uid):
    w = wizard[uid]; txt = update.message.text.strip()
    if w['step'] == 'token':
        if not re.match(r'^\d+:[A-Za-z0-9_-]{20,}$', txt):
            return await update.message.reply_text("❌ Token inválido.")
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
        await update.message.reply_text("*Passo 3/4* — Nome de exibição:", parse_mode='Markdown')
    elif w['step'] == 'nome':
        w['dados']['nome'] = txt; w['step'] = 'pix'
        await update.message.reply_text("*Passo 4/4* — Sua chave Pix:", parse_mode='Markdown')
    elif w['step'] == 'pix':
        w['dados']['pix'] = txt
        await finalizar(update, ctx, uid)

# ---------- WIZARD ATIVAR + RENOVAR ----------
ativ = {}

async def iniciar_ativar(update, ctx, uid):
    bots = sb_select('bots', ativo=True)
    if not bots:
        return await update.message.reply_text("❌ Nenhum bot cadastrado.")
    ativ[uid] = {'step': 'user', 'bots': {str(b['id']): b['id'] for b in bots}}
    await update.message.reply_text("🎯 *ATIVAR ASSINANTE*\n\n" + "\n".join(f"#{b['id']} — {b['nome_exibicao']}" for b in bots) + "\n\nDigite o NÚMERO do bot:", parse_mode='Markdown')

async def ativ_text(update, ctx, uid):
    a = ativ[uid]; txt = update.message.text.strip()
    if a['step'] == 'user':
        bid = a['bots'].get(txt.replace('#', ''))
        if not bid: return await update.message.reply_text("❌ Bot inválido.")
        a['bid'] = bid; a['step'] = 'dias'
        await update.message.reply_text("ID do cliente:")
    elif a['step'] == 'dias':
        if not txt.isdigit(): return await update.message.reply_text("❌ Só números.")
        a['uidc'] = int(txt); a['step'] = 'confirma'
        await update.message.reply_text("Por quantos dias?")
    elif a['step'] == 'confirma':
        dias = int(txt) if txt.isdigit() else 30
        exp = (datetime.now() + timedelta(days=dias)).isoformat()
        ex = first(sb_select('subscribers', bot_id=a['bid'], telegram_id=a['uidc']))
        if ex: sb_update('subscribers', ex['id'], {'status': 'active', 'data_expiracao': exp})
        else: sb_insert('subscribers', {'bot_id': a['bid'], 'telegram_id': a['uidc'], 'status': 'active', 'data_expiracao': exp})
        ativ.pop(uid)
        await update.message.reply_text(f"✅ Assinante ativado por {dias} dias!")

async def hub_renovar(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    p = update.message.text.split()
    bid = int(p[1]); dias = int(p[2]) if len(p) > 2 else 30
    b = bot_row(bid)
    if not b: return await update.message.reply_text("❌ Bot não encontrado.")
    sb_update('bots', bid, {'ativo': True, 'expira_em': (datetime.now() + timedelta(days=dias)).isoformat()})
    msg = f"✅ Bot #{bid} renovado +{dias}d."
    if b.get('creator_id'):
        cr = first(sb_select('creators', id=b['creator_id']))
        if cr and cr.get('indicado_por'):
            sb_insert('credits', {'creator_id': cr['indicado_por'], 'valor': 6.0, 'motivo': f'indicação bot #{bid}'})
            msg += " 💰 20% creditado pra afiliada."
    await update.message.reply_text(msg)

# ---------- HUB ----------
async def hub_start(update, ctx):
    uid = update.effective_user.id
    if uid == ADMIN_ID:
        return await update.message.reply_text("🛠️ *HUB ADMIN*\n\n/criarbot — wizard novo bot\n/ativar — wizard assinante\n/renovar bot_id [dias]\n/gerartoken email\n/bots", parse_mode='Markdown')
    await update.message.reply_text("⚒️ *PauloForge Soluções*\nTenha seu próprio bot no Telegram.", parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🤖 Criar meu bot", callback_data='criarbot')]]))

async def hub_cb(update, ctx):
    q = update.callback_query; await q.answer()
    if q.data == 'criarbot':
        await iniciar_wizard(q.message, ctx, q.from_user.id)

async def hub_criarbot(update, ctx):
    await iniciar_wizard(update, ctx, update.effective_user.id)

async def hub_ativar(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    await iniciar_ativar(update, ctx, update.effective_user.id)

async def hub_gerartoken(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    email = update.message.text.split(maxsplit=1)[1].strip().lower()
    c = first(sb_select('creators', email=email))
    cid = c['id'] if c else first(sb_insert('creators', {'email': email, 'link_code': str(secrets.randbelow(9000) + 1000)}))['id']
    tok = secrets.token_urlsafe(16)
    sb_insert('access_tokens', {'creator_id': cid, 'token': tok})
    await update.message.reply_text(f"🔑 Token do painel pra {email}:\n{tok}")

async def hub_bots(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    bots = sb_select('bots')
    await update.message.reply_text("\n".join(f"#{b['id']} {b['nome_exibicao']} ativo={b.get('ativo')}" for b in bots) or "Sem bots.")

# ---------- TAREFAS ----------
async def envia_codigos(app):
    while True:
        try:
            for lc in sb_select('login_codes', enviado=False):
                cs = first(sb_select('creators', id=lc['creator_id']))
                if cs and cs.get('telegram_id'):
                    prefix = "💸 Código de retirada:" if lc.get('tipo') == 'retirada' else "🔐 Código de acesso ao painel:"
                    await app.bot.send_message(cs['telegram_id'], f"{prefix} {lc['code']}")
                    sb_update('login_codes', lc['id'], {'enviado': True})
        except Exception as e: logger.warning(e)
        await asyncio.sleep(5)

async def drip_teasers(app):
    while True:
        try:
            limite = (datetime.now() - timedelta(hours=6)).isoformat()
            for ld in sb_select('leads'):
                if ld.get('ultimo_aviso') and ld['ultimo_aviso'] > limite: continue
                if is_active_sub(ld['bot_id'], ld['telegram_id']): continue
                b = bot_row(ld['bot_id'])
                if not b or not b.get('ativo'): continue
                teasers = sb_select('medias', bot_id=ld['bot_id'], tipo='teaser')
                if not teasers: continue
                t = teasers[secrets.randbelow(len(teasers))]
                try:
                    cap = (t['legenda'] or 'Olha o que você tá perdendo 👀') + "\n\nVem assinar e receber TUDO 😈"
                    if t['file_type'] == 'photo': await app.bot.send_photo(ld['telegram_id'], t['file_id'], caption=cap, reply_markup=btn_assinar(b))
                    else: await app.bot.send_video(ld['telegram_id'], t['file_id'], caption=cap, reply_markup=btn_assinar(b))
                except Exception as e: logger.warning(e)
                sb_update('leads', ld['id'], {'ultimo_aviso': datetime.now().isoformat()})
        except Exception as e: logger.warning(e)
        await asyncio.sleep(60)

async def checa_expiracao(app):
    while True:
        try:
            for b in sb_select('bots', ativo=True):
                if b.get('expira_em') and datetime.fromisoformat(b['expira_em']) < datetime.now():
                    await stop_bot_app(b['id'])
                    sb_update('bots', b['id'], {'ativo': False})
                    cs = first(sb_select('creators', id=b['creator_id'])) if b.get('creator_id') else None
                    if cs and cs.get('telegram_id'):
                        await app.bot.send_message(cs['telegram_id'], "⛔ Bot offline (plano vencido). Renove com o suporte.")
        except Exception as e: logger.warning(e)
        await asyncio.sleep(60)

async def vigia_bots(app):
    while True:
        try:
            for b in sb_select('bots', ativo=True):
                if b['id'] not in running:
                    try: await start_bot_app(b)
                    except Exception as e: logger.error(f"vigia bot {b['id']}: {e}")
        except Exception as e: logger.warning(e)
        await asyncio.sleep(20)

# ---------- MOTOR ----------
running = {}

async def start_bot_app(b):
    if b['id'] in running: return
    app = Application.builder().token(b['bot_token']).build()
    bid = b['id']
    app.add_handler(CommandHandler('start', make_start(bid)))
    app.add_handler(CommandHandler('stats', make_stats(bid)))
    app.add_handler(CommandHandler('vincular', make_vincular(bid)))
    app.add_handler(CallbackQueryHandler(make_cb(bid)))
    app.add_handler(MessageHandler((filters.PHOTO | filters.VIDEO), make_media(bid)))
    await app.initialize(); await app.start(); await app.updater.start_polling()
    running[bid] = app
    logger.info(f"🤖 Bot #{bid} {b['nome_exibicao']} online")

async def stop_bot_app(bid):
    app = running.pop(bid, None)
    if app:
        try:
            await app.updater.stop(); await app.stop(); await app.shutdown()
        except Exception: pass
        logger.info(f"🛑 Bot #{bid} offline")

async def post_init(application):
    asyncio.create_task(envia_codigos(application))
    asyncio.create_task(drip_teasers(application))
    asyncio.create_task(checa_expiracao(application))
    asyncio.create_task(vigia_bots(application))

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
    app.add_handler(CommandHandler('renovar', hub_renovar))
    app.add_handler(CommandHandler('gerartoken', hub_gerartoken))
    app.add_handler(CommandHandler('bots', hub_bots))
    app.add_handler(CallbackQueryHandler(hub_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    logger.info("🛠️ HUB PauloForge v5.1 online!")
    app.run_polling()

if __name__ == '__main__':
    main()
