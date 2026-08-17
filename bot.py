import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from supabase import create_client, Client

# Configuração (Vem das variáveis de ambiente do Railway/Render)
BOT_TOKEN = os.getenv('BOT_TOKEN')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

# Conecta ao Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Verifica se já é assinante ativo
    result = supabase.table('subscribers').select('*').eq('telegram_id', user_id).eq('status', 'active').execute()
    
    if result.data and len(result.data) > 0:
        await update.message.reply_text(
            "🔥 **BEM-VINDO DE VOLTA, VIP!**\n\nAcesse seu conteúdo exclusivo:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📸 Ver Conteúdo", callback_data='ver_conteudo')],
                [InlineKeyboardButton("💳 Minha Assinatura", callback_data='minha_assinatura')]
            ])
        )
    else:
        # Mostra o catálogo de modelos
        models = supabase.table('models').select('*').execute()
        
        if not models.data:
            await update.message.reply_text("🚧 Sistema em manutenção. Tente novamente em breve.")
            return

        texto = "🔞 **CONTEÚDO EXCLUSIVO**\n\nEscolha sua modelo favorita:\n\n"
        botoes = []
        
        for model in models.data:
            texto += f"👤 **{model['nome_exibicao']}**\n💰 R$ {model['preco_mensalidade']}/mês\n\n"
            botoes.append([InlineKeyboardButton(
                f"Assinar {model['nome_exibicao']}",
                callback_data=f'assinar_{model["id"]}'
            )])
        
        await update.message.reply_text(texto, reply_markup=InlineKeyboardMarkup(botoes))

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if query.data == 'ver_conteudo':
        sub = supabase.table('subscribers').select('model_id').eq('telegram_id', user_id).eq('status', 'active').execute()
        if sub.data:
            model_id = sub.data[0]['model_id']
            midias = supabase.table('medias').select('*').eq('model_id', model_id).execute()
            
            if not midias.data:
                await query.message.reply_text("📸 Novo conteúdo em breve!")
                return
                
            for midia in midias.data:
                if midia['file_type'] == 'photo':
                    await update.message.reply_photo(photo=midia['file_url'], caption=midia['legenda'])
                elif midia['file_type'] == 'video':
                    await update.message.reply_video(video=midia['file_url'], caption=midia['legenda'])
        else:
            await query.message.reply_text("⚠️ Assinatura não encontrada ou expirada.")

    elif query.data.startswith('assinar_'):
        model_id = int(query.data.split('_')[1])
        model = supabase.table('models').select('*').eq('id', model_id).execute()
        
        if model.data:
            m = model.data[0]
            texto = f"💳 **ASSINATURA: {m['nome_exibicao']}**\n\n"
            texto += f"Valor: R$ {m['preco_mensalidade']}/mês\n\n"
            texto += f"**Chave Pix:**\n`{m['chave_pix']}`\n\n"
            texto += "⚠️ *Após o pagamento, envie o comprovante no chat para ativação imediata.*"
            await query.message.reply_text(texto, parse_mode='Markdown')

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CallbackQueryHandler(menu_callback))
    
    logger.info("🤖 Bot PauloForge iniciado e rodando 24/7!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
