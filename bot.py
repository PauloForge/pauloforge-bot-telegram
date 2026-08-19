import os, re, logging, secrets, asyncio, time, json, threading, requests
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.getenv('BOT_TOKEN')
SB_URL = os.getenv('SUPABASE_URL')
SB_KEY = os.getenv('SUPABASE_KEY')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
PAINEL_URL = os.getenv('PAINEL_URL', '')
MP_TOKEN = os.getenv('MP_TOKEN', '')
WEBHOOK_URL = os.getenv('WEBHOOK_URL', '')
CRIAR_URL = os.getenv('CRIAR_URL', '') or ((PAINEL_URL.rstrip('/') + '/criarbot.html') if PAINEL_URL else '')
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

def tg_api(token, metodo, payload):
    return requests.post(f"https://api.telegram.org/bot{token}/{metodo}", json=payload).json()

def notify_admin(txt):
    if ADMIN_ID:
        try: tg_api(BOT_TOKEN, 'sendMessage', {'chat_id': ADMIN_ID, 'text': txt})
        except Exception as e: logger.warning(f'notify_admin: {e}')

def kb_assinar(b):
    rows = []
    if MP_TOKEN:
        rows.append([InlineKeyboardButton("💳 Pix copia-e-cola (libera na hora)", callback_data='assinar_mp')])
    rows.append([InlineKeyboardButton(f"💳 Assinar por R$ {b['preco']}/mês", url=f"{WA}?text=Quero%20assinar%20{b['nome_exibicao']}")])
    return InlineKeyboardMarkup(rows)

def mp_pix(valor, descricao, email):
    body = {"transaction_amount": float(valor), "description": descricao,
            "payment_method_id": "pix", "payer": {"email": email}}
    if WEBHOOK_URL.startswith('https://'):
        body["notification_url"] = WEBHOOK_URL
    r = requests.post("https://api.mercadopago.com/v1/payments",
        headers={"Authorization": f"Bearer {MP_TOKEN}", "X-Idempotency-Key": secrets.token_hex(16)},
        json=body).json()
    cop = (r.get('point_of_interaction') or {}).get('transaction_data', {}).get('qr_code')
    return cop, r

# ---------- WEBHOOK MERCADO PAGO + API ----------
def processa_pagamento(pid):
    try:
        r = requests.get(f"https://api.mercadopago.com/v1/payments/{pid}", headers={"Authorization": f"Bearer {MP_TOKEN}"}).json()
        if r.get('status') != 'approved': return
        pay = first(sb_select('payments', mp_id=pid))
        if not pay or pay.get('status') == 'approved': return
        sb_update('payments', pay['id'], {'status': 'approved'})
        desc = r.get('description') or ''
        if desc.startswith('PLANO_'):
            try: bid = int(desc.split('#')[1].split()[0])
            except Exception: bid = pay['bot_id']
            dias = 30 if 'mensal' in desc else 365
            sb_update('bots', bid, {'paid': True, 'ativo': True, 'expira_em': (datetime.now() + timedelta(days=dias)).isoformat()})
            b = bot_row(bid)
            if b and b.get('creator_id'):
                cr = first(sb_select('creators', id=b['creator_id']))
                if cr and cr.get('telegram_id'):
                    tg_api(BOT_TOKEN, 'sendMessage', {'chat_id': cr['telegram_id'], 'text': f"✅ Pagamento confirmado! Bot {b['nome_exibicao']} ativado por {dias} dias. 🎉"})
            notify_admin(f"✅ PIX confirmado: {desc} — R$ {pay['valor']} (ID {pay['telegram_id']})")
            return
        exp = (datetime.now() + timedelta(days=30)).isoformat()
        ex = first(sb_select('subscribers', bot_id=pay['bot_id'], telegram_id=pay['telegram_id']))
        if ex: sb_update('subscribers', ex['id'], {'status': 'active', 'data_expiracao': exp})
        else: sb_insert('subscribers', {'bot_id': pay['bot_id'], 'telegram_id': pay['telegram_id'], 'status': 'active', 'data_expiracao': exp})
        tg_api(BOT_TOKEN, 'sendMessage', {'chat_id': pay['telegram_id'], 'text': '✅ Pagamento confirmado! Acesso VIP liberado por 30 dias. 😈'})
        b = bot_row(pay['bot_id'])
        if b and b.get('creator_id'):
            cs = first(sb_select('creators', id=b['creator_id']))
            if cs:
                sb_insert('credits', {'creator_id': cs['id'], 'valor': round(float(pay['valor']) * 0.95, 2), 'motivo': f'assinatura bot #{b["id"]}'})
                if cs.get('telegram_id'):
                    tg_api(BOT_TOKEN, 'sendMessage', {'chat_id': cs['telegram_id'], 'text': f"💰 +1 assinante no bot {b['nome_exibicao']}! Valor já no seu saldo do painel."})
        notify_admin(f"✅ PIX confirmado: assinatura R$ {pay['valor']} no bot #{pay['bot_id']}")
        logger.info(f"💸 pagamento {pid} aprovado e liberado")
    except Exception as e:
        logger.error(f"webhook erro: {e}")

def api_criarbot(d):
    try:
        tid = str(d.get('tid', '')).strip()
        tok = str(d.get('tok', '')).strip()
        nome = (d.get('nome') or '').strip()
        pix = (d.get('pix') or '').strip()
        plano = d.get('plano', 'teste')
        if not tid.isdigit(): return {'msg': '❌ ID do Telegram inválido.'}
        tid = int(tid)
        if not re.match(r'^\d+:[A-Za-z0-9_-]{20,}$', tok): return {'msg': '❌ Token inválido.'}
        r = requests.get(f"https://api.telegram.org/bot{tok}/getMe").json()
        if not r.get('ok'): return {'msg': '❌ O Telegram recusou esse token.'}
        uname = r['result']['username']
        if first(sb_select('bots', bot_token=tok)): return {'msg': '❌ Esse bot já está conectado no sistema.'}
        cs = first(sb_select('creators', telegram_id=tid))
        if not cs:
            cs = first(sb_insert('creators', {'email': f'tg_{tid}@pauloforge.app', 'telegram_id': tid, 'nome': nome or uname, 'link_code': str(secrets.randbelow(9000) + 1000)}))
        cid = cs['id']
        if not cs.get('ref_code'): sb_update('creators', cid, {'ref_code': 'PF' + str(cid)})
        if plano == 'teste':
            if sb_select('login_codes', creator_id=cid, tipo='teste'):
                return {'msg': '⚠️ Você já usou o teste grátis.\nPra liberar, escolha R$ 30/mês ou R$ 150 único.'}
            code = str(secrets.randbelow(900000) + 100000)
            brow = first(sb_insert('bots', {'bot_token': tok, 'bot_username': uname, 'nome_exibicao': nome or uname, 'chave_pix': pix, 'creator_id': cid, 'ativo': True, 'paid': False, 'expira_em': (datetime.now() + timedelta(hours=12)).isoformat()}))
            sb_insert('login_codes', {'creator_id': cid, 'code': code, 'tipo': 'teste'})
            tokp = secrets.token_urlsafe(12)
            sb_insert('access_tokens', {'creator_id': cid, 'token': tokp})
            notify_admin(f"🎁 TESTE 12h ativado: @{uname} (ID {tid}) bot #{brow['id']}")
            return {'code': code, 'token_painel': tokp, 'msg': f'🎁 Teste 12h ativado!\nCódigo de confirmação: {code}\nToken do painel: {tokp}'}
        valor = 30.0 if plano == 'mensal' else 150.0
        brow = first(sb_insert('bots', {'bot_token': tok, 'bot_username': uname, 'nome_exibicao': nome or uname, 'chave_pix': pix, 'creator_id': cid, 'ativo': False, 'paid': False}))
        cop, mp = mp_pix(valor, f'PLANO_{plano} bot #{brow["id"]}', f'user_{tid}@pauloforge.app')
        if not cop:
            notify_admin(f"❌ MP falhou (plano {plano}): {str(mp)[:300]}")
            return {'msg': '❌ Falha ao gerar o Pix. Tente novamente.'}
        sb_insert('payments', {'mp_id': str(mp.get('id')), 'bot_id': brow['id'], 'telegram_id': tid, 'valor': valor})
        notify_admin(f"💠 QR Code gerado: PLANO {plano} R$ {valor} (ID {tid} @{uname})")
        return {'pix': cop, 'msg': '💰 Pague o Pix — seu bot ativa SOZINHO em até 1 min.'}
    except Exception as e:
        logger.exception('api_criarbot')
        return {'msg': f'❌ Erro interno: {e}'}

class WH(BaseHTTPRequestHandler):
    def _ok(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
    def _json(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers(); self.wfile.write(b)
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.end_headers()
    def do_POST(self):
        if self.path.startswith('/api/criarbot'):
            ln = int(self.headers.get('Content-Length') or 0)
            try: data = json.loads(self.rfile.read(ln)) if ln else {}
            except Exception: data = {}
            return self._json(api_criarbot(data))
        if self.path.startswith('/mp-webhook'):
            ln = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(ln) if ln else b'{}'
            try: data = json.loads(raw)
            except Exception: data = {}
            pid = None
            if isinstance(data, dict):
                pid = (data.get('data') or {}).get('id') or data.get('id')
            if pid:
                threading.Thread(target=processa_pagamento, args=(str(pid),), daemon=True).start()
            return self._ok()
        self._ok()
    def do_GET(self):
        self._ok()
    def log_message(self, *a): pass

def start_web_server():
    port = int(os.getenv('PORT', '8080'))
    srv = ThreadingHTTPServer(('0.0.0.0', port), WH)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    logger.info(f"🌐 Servidor webhook+API na porta {port}")

# ---------- BOTS DAS CRIADORAS ----------
def make_start(bid):
    async def h(update, ctx):
        b = bot_row(bid); uid = update.effective_user.id
        if not b: return
        s = b.get('settings') or {}
        if is_active_sub(bid, uid):
            await update.message.reply_text("🔥 VIP ativo! Aproveite 😈", parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📸 Ver conteúdo", callback_data='ver')]]))
            return
        if not first(sb_select('leads', bot_id=bid, telegram_id=uid)):
            sb_insert('leads', {'bot_id': bid, 'telegram_id': uid})
        kb = kb_assinar(b)
        if s.get('catalogo_on'):
            cats = sb_select('medias', bot_id=bid, tipo='catalogo')
            if cats:
                t = cats[secrets.randbelow(len(cats))]
                try: await ctx.bot.send_video(uid, t['file_id'], caption="Dá uma olhada no que te espera 👀", reply_markup=kb)
                except Exception: pass
        legenda = f"🔞 {b['nome_exibicao']}\n\nAssinatura: R$ {b['preco']}/mês\nPix: `{b['chave_pix']}`\n\nAssine e receba TUDO na hora 😈"
        if b.get('welcome_video'):
            try:
                await ctx.bot.send_video(uid, b['welcome_video'], caption=legenda, parse_mode='Markdown', reply_markup=kb)
            except Exception:
                await update.message.reply_text(legenda, parse_mode='Markdown', reply_markup=kb)
        else:
            await update.message.reply_text(legenda + "\n\nToque no botão pra assinar 👇", parse_mode='Markdown', reply_markup=kb)
        if s.get('grupo_on') and s.get('grupo_link'):
            gtxt = (s.get('grupo_msg') or 'Entre pro meu grupo VIP 😈') + f"\n\nTaxa de entrada: R$ {s.get('grupo_taxa') or b['preco']}\nPix: {b['chave_pix']}\n\nApós pagar, o acesso ao grupo é liberado."
            await update.message.reply_text(gtxt)
    return h

def make_cb(bid):
    async def h(update, ctx):
        q = update.callback_query; await q.answer(); uid = update.effective_user.id
        b = bot_row(bid)
        if q.data == 'assinar_mp':
            if not MP_TOKEN:
                return await q.message.reply_text("Pagamento online chegando. Use o botão do WhatsApp por enquanto.")
            wait = await q.message.reply_text("⏳ Gerando seu Pix...")
            cop, r = mp_pix(b['preco'], f"Assinatura {b['nome_exibicao']} 30 dias", f"user_{uid}@pauloforge.app")
            if not cop:
                notify_admin(f"❌ MP falhou (assinatura bot #{bid}): {str(r)[:300]}")
                return await wait.edit_text("❌ Falha ao gerar o Pix. Tente novamente.")
            sb_insert('payments', {'mp_id': str(r.get('id')), 'bot_id': bid, 'telegram_id': uid, 'valor': float(b['preco'])})
            notify_admin(f"💠 QR Code gerado: assinatura R$ {b['preco']} bot #{bid}")
            await wait.edit_text("📲 Pix copia e cola:\n\n`" + cop + "`\n\nPagou, liberou SOZINHO em até 1 min ✅", parse_mode='Markdown')
            return
        if not is_active_sub(bid, uid):
            return await q.message.reply_text("⚠️ Só assinantes. Toque abaixo 👇", reply_markup=kb_assinar(b))
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

# ---------- WIZARD CRIAR BOT (admin) ----------
wizard = {}
async def iniciar_wizard(update, ctx, uid):
    wizard[uid] = {'step': 'token', 'dados': {}, 'msgs': []}
    m = await update.message.reply_text("🤖 VAMOS CRIAR SEU BOT!\n\nPasso 1/4 — Abra o @BotFather, crie um bot e cole aqui o TOKEN dele:", parse_mode='Markdown')
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
            sb_update('bots', brow['id'], {'creator_id': cid, 'nome_exibicao': d['nome'], 'chave_pix': d['pix'], 'ativo': True, 'expira_em': (datetime.now() + timedelta(hours=12)).isoformat()})
        else:
            brow = first(sb_insert('bots', {'bot_token': d['token'], 'bot_username': d['username'], 'nome_exibicao': d['nome'], 'chave_pix': d['pix'], 'creator_id': cid, 'ativo': True, 'expira_em': (datetime.now() + timedelta(hours=12)).isoformat()}))
        sb_insert('login_codes', {'creator_id': cid, 'code': str(secrets.randbelow(900000) + 100000), 'tipo': 'teste'})
        tok = secrets.token_urlsafe(12)
        sb_insert('access_tokens', {'creator_id': cid, 'token': tok})
        notify_admin(f"🆕 Bot criado via wizard: @{d['username']} (ID {uid})")
        for mid in w['msgs']:
            try: await ctx.bot.delete_message(uid, mid)
            except Exception: pass
        try: await wait.delete()
        except Exception: pass
        await ctx.bot.send_message(uid,
            f"🎉 PRONTO, {d['nome']}!\n\n🤖 Seu bot: @{d['username']} (id {brow['id']})\n🔑 Token do painel: {tok}\n📧 Email: {email}\n🌐 Painel: {PAINEL_URL or 'em breve'}\n🎁 Teste grátis: 12h.\n\n📖 O worker liga seu bot em até 20s!")
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
        await update.message.reply_text("✅ Token válido!\n\nPasso 2/4 — Seu melhor e-mail:", parse_mode='Markdown')
    elif w['step'] == 'email':
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', txt):
            return await update.message.reply_text("❌ E-mail inválido.")
        w['dados']['email'] = txt; w['step'] = 'nome'
        await update.message.reply_text("Passo 3/4 — Nome de exibição:", parse_mode='Markdown')
    elif w['step'] == 'nome':
        w['dados']['nome'] = txt; w['step'] = 'pix'
        await update.message.reply_text("Passo 4/4 — Sua chave Pix:", parse_mode='Markdown')
    elif w['step'] == 'pix':
        w['dados']['pix'] = txt
        await finalizar(update, ctx, uid)

# ---------- WIZARD ATIVAR + ADMIN ----------
ativ = {}
async def iniciar_ativar(update, ctx, uid):
    bots = sb_select('bots', ativo=True)
    if not bots:
        return await update.message.reply_text("❌ Nenhum bot cadastrado.")
    ativ[uid] = {'step': 'user', 'bots': {str(b['id']): b['id'] for b in bots}}
    await update.message.reply_text("🎯 ATIVAR ASSINANTE\n\n" + "\n".join(f"#{b['id']} — {b['nome_exibicao']}" for b in bots) + "\n\nDigite o NÚMERO do bot:", parse_mode='Markdown')

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
    sb_update('bots', bid, {'ativo': True, 'paid': True, 'expira_em': (datetime.now() + timedelta(days=dias)).isoformat()})
    msg = f"✅ Bot #{bid} renovado +{dias}d (PAGO)."
    if b.get('creator_id'):
        cr = first(sb_select('creators', id=b['creator_id']))
        if cr and cr.get('indicado_por'):
            sb_insert('credits', {'creator_id': cr['indicado_por'], 'valor': 6.0, 'motivo': f'indicação bot #{bid}'})
            msg += " 💰 20% creditado pra afiliada."
    await update.message.reply_text(msg)

async def hub_liberagrupo(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    p = update.message.text.split()
    bid, uid = int(p[1]), int(p[2])
    b = bot_row(bid)
    if not b: return await update.message.reply_text("❌ Bot não encontrado.")
    s = b.get('settings') or {}
    link = s.get('grupo_link')
    if not link: return await update.message.reply_text("❌ Esse bot não tem grupo configurado.")
    tg_api(b['bot_token'], 'sendMessage', {'chat_id': uid, 'text': f"🔓 Acesso liberado! Link do grupo VIP:\n{link}"})
    await update.message.reply_text(f"✅ Link do grupo enviado pro cliente {uid}.")

async def hub_start(update, ctx):
    uid = update.effective_user.id
    if uid == ADMIN_ID:
        return await update.message.reply_text("🛠️ HUB ADMIN\n\n/criarbot — wizard novo bot\n/ativar — wizard assinante\n/renovar bot_id dias\n/liberagrupo bot_id user_id\n/gerartoken email\n/bots")
    if CRIAR_URL:
        return await update.message.reply_text("⚒️ PauloForge Soluções\nTenha seu próprio bot no Telegram.\n\n🎁 Teste grátis 12h · R$ 30/mês · R$ 150 único\n\nToque pra criar o seu em 1 minuto 👇",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🤖 Criar meu bot", url=CRIAR_URL)]]))
    await update.message.reply_text("⚒️ PauloForge Soluções\nTenha seu próprio bot no Telegram.",
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
    await update.message.reply_text("\n".join(f"#{b['id']} {b['nome_exibicao']} ativo={b.get('ativo')} paid={b.get('paid')}" for b in bots) or "Sem bots.")

async def error_handler(update, ctx):
    logger.error(f"💥 ERRO DE HANDLER: {ctx.error}")

# ---------- TAREFAS ----------
async def envia_codigos(app):
    while True:
        try:
            for lc in sb_select('login_codes', enviado=False):
                cs = first(sb_select('creators', id=lc['creator_id']))
                if cs and cs.get('telegram_id'):
                    prefix = "💸 Código de retirada:" if lc.get('tipo') == 'retirada' else ("🔑 Código de troca de token:" if lc.get('tipo') == 'token' else ("🎁 Código do MODO TESTE:" if lc.get('tipo') == 'teste' else "🔐 Código de acesso ao painel:"))
                    await app.bot.send_message(cs['telegram_id'], f"{prefix} {lc['code']}")
                sb_update('login_codes', lc['id'], {'enviado': True})
        except Exception as e: logger.warning(e)
        await asyncio.sleep(5)

async def drip_teasers(app):
    while True:
        try:
            for ld in sb_select('leads'):
                b = bot_row(ld['bot_id'])
                if not b or not b.get('ativo'): continue
                s = b.get('settings') or {}
                if s.get('teaser_on') is False: continue
                horas = int(s.get('teaser_h') or 6)
                if ld.get('ultimo_aviso') and ld['ultimo_aviso'] > (datetime.now() - timedelta(hours=horas)).isoformat(): continue
                if is_active_sub(ld['bot_id'], ld['telegram_id']): continue
                teasers = sb_select('medias', bot_id=ld['bot_id'], tipo='teaser')
                if not teasers: continue
                t = teasers[secrets.randbelow(len(teasers))]
                try:
                    cap = (t['legenda'] or 'Olha o que você tá perdendo 👀') + "\n\nVem assinar e receber TUDO 😈"
                    if t['file_type'] == 'photo': await app.bot.send_photo(ld['telegram_id'], t['file_id'], caption=cap, reply_markup=kb_assinar(b))
                    else: await app.bot.send_video(ld['telegram_id'], t['file_id'], caption=cap, reply_markup=kb_assinar(b))
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

async def resumo_periodico(app):
    await asyncio.sleep(20)
    while True:
        try:
            bots = sb_select('bots')
            ativos = [b for b in bots if b.get('ativo')]
            pagos = [b for b in bots if b.get('paid')]
            subs = sb_select('subscribers', status='active')
            vends = [c for c in sb_select('creators') if c.get('telegram_id')]
            await app.bot.send_message(ADMIN_ID,
                f"📊 RESUMO PauloForge\n👥 vendedoras ativas: {len(vends)}\n🤖 bots ativos: {len(ativos)} (pagos: {len(pagos)})\n💎 assinantes ativos: {len(subs)}")
        except Exception as e: logger.warning(e)
        await asyncio.sleep(6 * 3600)

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
    app.add_error_handler(error_handler)
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
    start_web_server()
    asyncio.create_task(envia_codigos(application))
    asyncio.create_task(drip_teasers(application))
    asyncio.create_task(checa_expiracao(application))
    asyncio.create_task(vigia_bots(application))
    asyncio.create_task(resumo_periodico(application))

async def on_text(update, ctx):
    uid = update.effective_user.id
    if uid in wizard: return await wizard_text(update, ctx, uid)
    if uid in ativ and uid == ADMIN_ID: return await ativ_text(update, ctx, uid)

def main():
    while True:
        try:
            app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
            app.add_handler(CommandHandler('start', hub_start))
            app.add_handler(CommandHandler('criarbot', hub_criarbot))
            app.add_handler(CommandHandler('novabot', hub_criarbot))
            app.add_handler(CommandHandler('ativar', hub_ativar))
            app.add_handler(CommandHandler('renovar', hub_renovar))
            app.add_handler(CommandHandler('liberagrupo', hub_liberagrupo))
            app.add_handler(CommandHandler('gerartoken', hub_gerartoken))
            app.add_handler(CommandHandler('bots', hub_bots))
            app.add_handler(CallbackQueryHandler(hub_cb))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
            app.add_error_handler(error_handler)
            logger.info("🛠️ HUB PauloForge v8.1 online!")
            app.run_polling()
        except Exception as e:
            logger.error(f"💥 Hub caiu ({e}) — reiniciando em 5s...")
            time.sleep(5)

if __name__ == '__main__':
    main()
