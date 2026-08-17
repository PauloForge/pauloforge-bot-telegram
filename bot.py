import os
import logging
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

BOT_TOKEN = os.getenv('BOT_TOKEN')
SB_URL = os.getenv('SUPABASE_URL')
SB_KEY = os.getenv('SUPABASE_KEY')

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

def sb_select(table, **filtros):
    headers = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
    params = {col: f"eq.{val}" for col, val in filtros.items()}
    r = requests.get(f"{SB_URL}/rest/v1/{table}", headers=headers, params=params)
    r.raise_for_status()
    return r.json()

async def start(update, context):
    user_id = update.effective_user.id
    subs = sb_select('subscribers', telegram_id=user_id, status='active')

    if subs:
        await update.message.reply_text(
            "🔥 *BEM-VINDO DE VOLTA, VIP!*\n\nAcesse seu conteúdo:",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📸 Ver Conteúdo", callback_data='ver_conteudo')]
            ])
        )
    else:
        models = sb_select('models')
        if not models:
            await update.message.reply_text("🚧 Catálogo sendo montado. Volte em breve!")
            return
        texto = "🔞 *CONTEÚDO EXCLUSIVO*\n\nEscolha sua modelo:\n\n"
        botoes = []
        for m in models:
            texto += f"👤 *{m['nome_exibicao']}*\n💰 R$ {m['preco_mensalidade']}/mês\n\n"
            botoes.append([InlineKeyboardButton(f"Assinar {m['nome_exibicao']}", callback_data=f"assinar_{m['id']}")])
        await update.message.reply_text(texto, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(botoes))

async def menu_callback(update, context):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if query.data == 'ver_conteudo':
        subs = sb_select('subscribers', telegram_id=user_id, status='active')
        if not subs:
            await query.message.reply_text("⚠️ Assinatura não encontrada ou expirada.")
            return
        midias = sb_select('medias', model_id=subs[0]['model_id'])
        if not midias:
            await query.message.reply_text("📸 Novo conteúdo em breve!")
            return
        for midia in midias:
            if midia['file_type'] == 'photo':
                await query.message.reply_photo(photo=midia['file_url'], caption=midia['legenda'])
            else:
                await query.message.reply_video(video=midia['file_url'], caption=midia['legenda'])

    elif query.data.startswith('assinar_'):
        model_id = int(query.data.split('_')[1])
        models = sb_select('models', id=model_id)
        if models:
            m = models[0]
            texto = (f"💳 *ASSINATURA: {m['nome_exibicao']}*\n\n"
                     f"Valor: R$ {m['preco_mensalidade']}/mês\n\n"
                     f"*Chave Pix:*\n`{m['chave_pix']}`\n\n"
                     "Após pagar, envie o comprovante pra ativação.")
            await query.message.reply_text(texto, parse_mode='Markdown')

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CallbackQueryHandler(menu_callback))
    logger.info("🤖 Bot PauloForge online 24/7!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
