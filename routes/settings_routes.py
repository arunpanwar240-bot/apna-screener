from flask import Blueprint, render_template, request, redirect, url_for, flash
from config import load_config, save_config

settings_routes = Blueprint('settings_routes', __name__)
config = load_config()

@settings_routes.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        config["client_id"] = request.form.get('client_id', '').strip()
        config["access_token"] = request.form.get('access_token', '').strip()
        config["telegram_bot_token"] = request.form.get('telegram_bot_token', '').strip()
        config["telegram_chat_id"] = request.form.get('telegram_chat_id', '').strip()
        save_config(config)
        flash("✅ Settings saved successfully!")
        return redirect(url_for("settings_routes.settings"))
    return render_template("settings.html", config=config)
