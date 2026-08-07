import os, json, base64, threading, webbrowser, secrets, smtplib, time
from collections import defaultdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
_secret_key = os.environ.get("SECRET_KEY")
if not _secret_key:
    raise RuntimeError("SECRET_KEY não definida. Configure a variável de ambiente antes de iniciar.")
app.secret_key = _secret_key

db_url = os.environ.get("DATABASE_URL", "sqlite:///app.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "a01affonso@gmail.com")
MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")

db = SQLAlchemy(app)

# ── Rate limiting ─────────────────────────────────────────────────────────────
_rate_buckets: dict = defaultdict(list)

def rate_limit_ok(chave: str, max_req: int = 5, janela: int = 300) -> bool:
    agora = time.time()
    _rate_buckets[chave] = [t for t in _rate_buckets[chave] if agora - t < janela]
    if len(_rate_buckets[chave]) >= max_req:
        return False
    _rate_buckets[chave].append(agora)
    return True



# ── Models ────────────────────────────────────────────────────────────────────

class Tenant(db.Model):
    __tablename__ = "tenant"
    id            = db.Column(db.Integer, primary_key=True)
    nome_negocio  = db.Column(db.String(200), nullable=False)
    logo_base64   = db.Column(db.Text)
    status        = db.Column(db.String(20), default="trial")
    data_cadastro = db.Column(db.DateTime, default=datetime.utcnow)
    data_expiracao = db.Column(db.DateTime)

    def dias_restantes(self):
        if not self.data_expiracao:
            return None
        delta = self.data_expiracao - datetime.utcnow()
        return max(0, delta.days)

    def esta_ativo(self):
        if self.status == "ativo":
            return True
        if self.status == "trial" and self.data_expiracao and self.data_expiracao > datetime.utcnow():
            return True
        return False

class Usuario(db.Model):
    __tablename__  = "usuario"
    id             = db.Column(db.Integer, primary_key=True)
    tenant_id      = db.Column(db.Integer, db.ForeignKey("tenant.id"), nullable=True)
    email          = db.Column(db.String(200), unique=True, nullable=False)
    senha          = db.Column(db.String(256), nullable=False)
    nome           = db.Column(db.String(200))
    is_admin       = db.Column(db.Boolean, default=False)
    is_super_admin = db.Column(db.Boolean, default=False)
    ultimo_acesso  = db.Column(db.DateTime)

class PasswordResetToken(db.Model):
    __tablename__ = "password_reset_token"
    id         = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuario.id"), nullable=False)
    token      = db.Column(db.String(120), unique=True, nullable=False)
    expiracao  = db.Column(db.DateTime, nullable=False)
    usado      = db.Column(db.Boolean, default=False)

class Configuracao(db.Model):
    __tablename__         = "configuracao"
    id                    = db.Column(db.Integer, primary_key=True)
    tenant_id             = db.Column(db.Integer, db.ForeignKey("tenant.id"), unique=True, nullable=False)
    nome_clinica          = db.Column(db.String(200), default="Minha Clínica")
    regime_tributario     = db.Column(db.String(50), default="")
    pro_labore            = db.Column(db.Float, default=0)
    horas_mes             = db.Column(db.Float, default=160)
    lucro_desejado        = db.Column(db.Float, default=30)
    custos_fixos_json     = db.Column(db.Text, default="[]")
    custos_variaveis_json = db.Column(db.Text, default="[]")

class Insumo(db.Model):
    __tablename__   = "insumo"
    id              = db.Column(db.Integer, primary_key=True)
    tenant_id       = db.Column(db.Integer, db.ForeignKey("tenant.id"), nullable=False)
    nome            = db.Column(db.String(200), nullable=False)
    unidade         = db.Column(db.String(20))
    qtd_embalagem   = db.Column(db.Float, default=1)
    custo_embalagem = db.Column(db.Float, default=0)

    @property
    def custo_unitario(self):
        if self.qtd_embalagem and self.qtd_embalagem > 0:
            return self.custo_embalagem / self.qtd_embalagem
        return 0

class Protocolo(db.Model):
    __tablename__ = "protocolo"
    id            = db.Column(db.Integer, primary_key=True)
    tenant_id     = db.Column(db.Integer, db.ForeignKey("tenant.id"), nullable=False)
    nome          = db.Column(db.String(200), nullable=False)
    itens_json    = db.Column(db.Text, default="[]")
    horas_clinica = db.Column(db.Float, default=0)
    preco1        = db.Column(db.Float, default=0)
    preco2        = db.Column(db.Float, default=0)
    preco3        = db.Column(db.Float, default=0)

# ── Helpers ───────────────────────────────────────────────────────────────────

def calcular_hora_clinica(config):
    if not config or not config.horas_mes:
        return 0, 0
    fixos     = json.loads(config.custos_fixos_json or "[]")
    variaveis = json.loads(config.custos_variaveis_json or "[]")
    total     = sum(c["valor"] for c in fixos + variaveis) + (config.pro_labore or 0)
    hora      = total / config.horas_mes
    hora_lucro = hora * (1 + (config.lucro_desejado or 0) / 100)
    return hora, hora_lucro

def formatar_horas(h):
    if not h or h == 0: return "—"
    horas = int(h)
    mins  = int(round((h - horas) * 60))
    if horas == 0: return f"{mins}min"
    if mins  == 0: return f"{horas}h"
    return f"{horas}h{mins:02d}min"

@app.template_filter("brl")
def brl_filter(value):
    try:
        v = float(value or 0)
        s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return f"R$ {s}"
    except Exception:
        return "R$ 0,00"

@app.template_filter("qtd")
def qtd_filter(value):
    try:
        v = float(value or 0)
        return str(int(v)) if v == int(v) else f"{v:.3g}".replace(".", ",")
    except Exception:
        return "0"

app.jinja_env.globals["formatar_horas"] = formatar_horas

# ── CSRF ──────────────────────────────────────────────────────────────────────

def gerar_csrf_token():
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_hex(32)
    return session["_csrf_token"]

app.jinja_env.globals["csrf_token"] = gerar_csrf_token

@app.before_request
def verificar_csrf():
    if request.method == "POST":
        token = session.get("_csrf_token")
        if not token or token != request.form.get("_csrf_token"):
            abort(403)

# ── Security headers ──────────────────────────────────────────────────────────

@app.after_request
def add_security_headers(resp):
    resp.headers["X-Content-Type-Options"]  = "nosniff"
    resp.headers["X-Frame-Options"]         = "SAMEORIGIN"
    resp.headers["X-XSS-Protection"]        = "1; mode=block"
    resp.headers["Referrer-Policy"]         = "strict-origin-when-cross-origin"
    resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp

# ── Context Processor ─────────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    nome_clinica   = "Minha Clínica"
    logo_base64    = None
    dias_restantes = None

    if session.get("tenant_id"):
        tid    = session["tenant_id"]
        tenant = db.session.get(Tenant, tid)
        if tenant:
            logo_base64 = tenant.logo_base64
            if tenant.status == "trial":
                dias_restantes = tenant.dias_restantes()
        config = Configuracao.query.filter_by(tenant_id=tid).first()
        if config and config.nome_clinica:
            nome_clinica = config.nome_clinica

    return dict(nome_clinica=nome_clinica, logo_base64=logo_base64,
                dias_restantes=dias_restantes)

# ── Decorators ────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "usuario_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def super_admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_super_admin"):
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated

def tenant_ativo(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("is_super_admin"):
            return f(*args, **kwargs)
        tid = session.get("tenant_id")
        if not tid:
            return redirect(url_for("login"))
        tenant = db.session.get(Tenant, tid)
        if not tenant or not tenant.esta_ativo():
            return redirect(url_for("expirado"))
        return f(*args, **kwargs)
    return decorated

# ── Registro ──────────────────────────────────────────────────────────────────

@app.route("/registro", methods=["GET", "POST"])
def registro():
    if request.method == "POST":
        nome  = request.form.get("nome_negocio", "").strip()
        email = request.form.get("email", "").strip().lower()
        senha = request.form.get("senha", "")
        conf  = request.form.get("confirmar_senha", "")

        if not nome or not email or not senha:
            flash("Preencha todos os campos obrigatórios.", "error")
            return render_template("registro.html")
        if senha != conf:
            flash("As senhas não coincidem.", "error")
            return render_template("registro.html")
        if len(senha) < 6:
            flash("A senha deve ter pelo menos 6 caracteres.", "error")
            return render_template("registro.html")
        if Usuario.query.filter_by(email=email).first():
            flash("Este e-mail já está cadastrado.", "error")
            return render_template("registro.html")

        tenant = Tenant(nome_negocio=nome, status="inativo")
        db.session.add(tenant)
        db.session.flush()

        usuario = Usuario(tenant_id=tenant.id, email=email,
                          senha=generate_password_hash(senha), nome=nome,
                          is_admin=True)
        db.session.add(usuario)

        config = Configuracao(tenant_id=tenant.id, nome_clinica=nome)
        db.session.add(config)
        db.session.commit()

        flash("Cadastro realizado! Sua conta será ativada pela A'01 Negócios em breve.", "success")
        return redirect(url_for("login"))

    return render_template("registro.html")

# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
def login():
    if "usuario_id" in session:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        if not rate_limit_ok(f"login:{request.remote_addr}", max_req=10, janela=300):
            flash("Muitas tentativas. Aguarde 5 minutos e tente novamente.", "error")
            return render_template("login.html")
        email   = request.form.get("email", "").strip().lower()
        senha   = request.form.get("senha", "")
        usuario = Usuario.query.filter_by(email=email).first()

        if not usuario or not check_password_hash(usuario.senha, senha):
            flash("E-mail ou senha incorretos.", "error")
            return render_template("login.html")

        if not usuario.is_super_admin:
            tenant = db.session.get(Tenant, usuario.tenant_id)
            if not tenant or not tenant.esta_ativo():
                flash("Sua conta ainda não foi ativada. Entre em contato com a A'01 Negócios.", "error")
                return render_template("login.html")

        usuario.ultimo_acesso = datetime.utcnow()
        db.session.commit()

        session["usuario_id"]     = usuario.id
        session["tenant_id"]      = usuario.tenant_id
        session["is_admin"]       = usuario.is_admin
        session["is_super_admin"] = usuario.is_super_admin
        return redirect(url_for("dashboard"))

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/expirado")
def expirado():
    return render_template("expirado.html")

@app.route("/esqueci-senha", methods=["GET", "POST"])
def esqueci_senha():
    if request.method == "POST":
        if not rate_limit_ok(f"reset:{request.remote_addr}", max_req=5, janela=300):
            flash("Muitas solicitações. Aguarde 5 minutos e tente novamente.", "error")
            return redirect(url_for("esqueci_senha"))
        email   = request.form.get("email", "").strip().lower()
        usuario = Usuario.query.filter_by(email=email).first()
        if usuario:
            PasswordResetToken.query.filter_by(usuario_id=usuario.id).delete()
            token     = secrets.token_urlsafe(40)
            expiracao = datetime.utcnow() + timedelta(hours=1)
            db.session.add(PasswordResetToken(
                usuario_id=usuario.id, token=token, expiracao=expiracao))
            db.session.commit()
            link = url_for("resetar_senha", token=token, _external=True)
            try:
                html_body = f"""
                <div style="font-family:system-ui,sans-serif;max-width:480px;margin:0 auto;padding:32px 24px">
                  <p style="font-size:15px;color:#1A2E38">Olá!</p>
                  <p style="font-size:14px;color:#5A7A8A;margin-bottom:24px">
                    Recebemos uma solicitação para redefinir a senha da sua conta.
                    Clique no botão abaixo — o link é válido por <strong>1 hora</strong>.
                  </p>
                  <a href="{link}"
                     style="display:inline-block;background:#0F3A4A;color:white;padding:12px 28px;border-radius:8px;font-size:15px;font-weight:600;text-decoration:none">
                    Redefinir minha senha
                  </a>
                  <p style="font-size:12px;color:#8A9AAA;margin-top:24px">
                    Se você não solicitou isso, ignore este e-mail. Nenhuma alteração foi feita.
                  </p>
                  <hr style="border:none;border-top:1px solid #D0DDE3;margin:24px 0">
                  <p style="font-size:12px;color:#8A9AAA">A'01 Negócios — Precificação de Serviços</p>
                </div>"""
                msg = MIMEMultipart("alternative")
                msg["Subject"] = "Redefinição de senha — Precificação de Serviços"
                msg["From"]    = MAIL_USERNAME
                msg["To"]      = email
                msg.attach(MIMEText(html_body, "html", "utf-8"))
                with smtplib.SMTP("smtp.gmail.com", 587) as server:
                    server.starttls()
                    server.login(MAIL_USERNAME, MAIL_PASSWORD)
                    server.sendmail(MAIL_USERNAME, email, msg.as_string())
            except Exception:
                pass
        flash("Se o e-mail estiver cadastrado, você receberá um link em instantes.", "success")
        return redirect(url_for("esqueci_senha"))
    return render_template("esqueci_senha.html")

@app.route("/resetar-senha/<token>", methods=["GET", "POST"])
def resetar_senha(token):
    reset = PasswordResetToken.query.filter_by(token=token, usado=False).first()
    if not reset or reset.expiracao < datetime.utcnow():
        flash("Link inválido ou expirado. Solicite um novo.", "error")
        return redirect(url_for("esqueci_senha"))
    if request.method == "POST":
        senha = request.form.get("senha", "")
        conf  = request.form.get("confirmar_senha", "")
        if len(senha) < 6:
            flash("A senha deve ter pelo menos 6 caracteres.", "error")
            return render_template("resetar_senha.html", token=token)
        if senha != conf:
            flash("As senhas não coincidem.", "error")
            return render_template("resetar_senha.html", token=token)
        usuario = db.session.get(Usuario, reset.usuario_id)
        usuario.senha = generate_password_hash(senha)
        reset.usado   = True
        db.session.commit()
        flash("Senha redefinida com sucesso! Faça login.", "success")
        return redirect(url_for("login"))
    return render_template("resetar_senha.html", token=token)

# ── Painel A'01 (super admin) ─────────────────────────────────────────────────

@app.route("/painel-a01")
@login_required
@super_admin_required
def painel_a01():
    tenants  = Tenant.query.order_by(Tenant.data_cadastro.desc()).all()
    usuarios = {u.tenant_id: u for u in Usuario.query.filter_by(is_admin=True).all()}
    data = []
    for t in tenants:
        u = usuarios.get(t.id)
        data.append({
            "tenant":         t,
            "email_admin":    u.email if u else "—",
            "ultimo_acesso":  u.ultimo_acesso if u else None,
            "dias_restantes": t.dias_restantes(),
        })
    total_ativos    = sum(1 for t in tenants if t.status == "ativo")
    total_trial     = sum(1 for t in tenants if t.status == "trial" and t.esta_ativo())
    total_expirados = sum(1 for t in tenants if not t.esta_ativo())
    return render_template("painel_a01.html", data=data,
                           total_ativos=total_ativos, total_trial=total_trial,
                           total_expirados=total_expirados)

@app.route("/painel-a01/ativar/<int:tid>", methods=["POST"])
@login_required
@super_admin_required
def ativar_tenant(tid):
    t = db.session.get(Tenant, tid)
    if t:
        t.status = "ativo"
        t.data_expiracao = None
        db.session.commit()
        flash(f"{t.nome_negocio} ativado com sucesso.", "success")
    return redirect(url_for("painel_a01"))

@app.route("/painel-a01/desativar/<int:tid>", methods=["POST"])
@login_required
@super_admin_required
def desativar_tenant(tid):
    t = db.session.get(Tenant, tid)
    if t:
        t.status = "inativo"
        db.session.commit()
        flash(f"{t.nome_negocio} desativado.", "warning")
    return redirect(url_for("painel_a01"))

# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
@tenant_ativo
def dashboard():
    tid    = session["tenant_id"]
    config = Configuracao.query.filter_by(tenant_id=tid).first()
    _, hora_lucro    = calcular_hora_clinica(config)
    total_protocolos = Protocolo.query.filter_by(tenant_id=tid).count()
    total_insumos    = Insumo.query.filter_by(tenant_id=tid).count()
    return render_template("dashboard.html", hora_lucro=hora_lucro,
                           total_protocolos=total_protocolos,
                           total_insumos=total_insumos, config=config)

# ── Configurações ─────────────────────────────────────────────────────────────

@app.route("/configuracoes", methods=["GET", "POST"])
@login_required
@tenant_ativo
def configuracoes():
    if session.get("is_super_admin"):
        return redirect(url_for("painel_a01"))
    tid    = session["tenant_id"]
    tenant = db.session.get(Tenant, tid)
    config = Configuracao.query.filter_by(tenant_id=tid).first()
    if not config:
        config = Configuracao(tenant_id=tid)
        db.session.add(config)
        db.session.commit()

    if request.method == "POST":
        config.nome_clinica       = request.form.get("nome_clinica", "").strip()
        config.regime_tributario  = request.form.get("regime_tributario", "")
        config.pro_labore         = float(request.form.get("pro_labore", 0) or 0)
        config.horas_mes      = float(request.form.get("horas_mes", 160) or 160)
        config.lucro_desejado = float(request.form.get("lucro_desejado", 30) or 30)

        nomes_f   = request.form.getlist("fixo_nome")
        valores_f = request.form.getlist("fixo_valor")
        config.custos_fixos_json = json.dumps(
            [{"nome": n, "valor": float(v or 0)} for n, v in zip(nomes_f, valores_f) if n.strip()])

        nomes_v   = request.form.getlist("var_nome")
        valores_v = request.form.getlist("var_valor")
        config.custos_variaveis_json = json.dumps(
            [{"nome": n, "valor": float(v or 0)} for n, v in zip(nomes_v, valores_v) if n.strip()])

        logo_file = request.files.get("logo")
        if logo_file and logo_file.filename:
            data = logo_file.read()
            if len(data) <= 1 * 1024 * 1024:
                mime = logo_file.mimetype or "image/png"
                tenant.logo_base64 = f"data:{mime};base64,{base64.b64encode(data).decode()}"

        if request.form.get("remover_logo") == "1":
            tenant.logo_base64 = None

        db.session.commit()
        flash("Configurações salvas!", "success")
        return redirect(url_for("configuracoes"))

    fixos     = json.loads(config.custos_fixos_json or "[]")
    variaveis = json.loads(config.custos_variaveis_json or "[]")
    hora_clinica, hora_com_lucro = calcular_hora_clinica(config)
    total_fixos     = sum(c["valor"] for c in fixos)
    total_variaveis = sum(c["valor"] for c in variaveis)
    total_custos    = total_fixos + total_variaveis + (config.pro_labore or 0)

    return render_template("configuracoes.html", config=config, tenant=tenant,
                           fixos=fixos, variaveis=variaveis,
                           hora_clinica=hora_clinica, hora_com_lucro=hora_com_lucro,
                           total_fixos=total_fixos, total_variaveis=total_variaveis,
                           total_custos=total_custos)

# ── Insumos ───────────────────────────────────────────────────────────────────

@app.route("/insumos")
@login_required
@tenant_ativo
def insumos():
    tid   = session["tenant_id"]
    lista = Insumo.query.filter_by(tenant_id=tid).order_by(Insumo.nome).all()
    return render_template("insumos.html", insumos=lista)

@app.route("/insumos/salvar", methods=["POST"])
@login_required
@tenant_ativo
def insumo_salvar():
    tid   = session["tenant_id"]
    iid   = request.form.get("id")
    nome  = request.form.get("nome", "").strip()
    un    = request.form.get("unidade", "")
    qtd   = float(request.form.get("qtd_embalagem", 1) or 1)
    custo = float(request.form.get("custo_embalagem", 0) or 0)
    if iid:
        i = db.session.get(Insumo, int(iid))
        if i and i.tenant_id == tid:
            i.nome = nome; i.unidade = un
            i.qtd_embalagem = qtd; i.custo_embalagem = custo
    else:
        db.session.add(Insumo(tenant_id=tid, nome=nome, unidade=un,
                              qtd_embalagem=qtd, custo_embalagem=custo))
    db.session.commit()
    return redirect(url_for("insumos"))

@app.route("/insumos/excluir/<int:id>")
@login_required
@tenant_ativo
def insumo_excluir(id):
    tid = session["tenant_id"]
    i   = db.session.get(Insumo, id)
    if i and i.tenant_id == tid:
        db.session.delete(i); db.session.commit()
    return redirect(url_for("insumos"))

# ── Protocolos ────────────────────────────────────────────────────────────────

@app.route("/protocolos")
@login_required
@tenant_ativo
def protocolos():
    tid     = session["tenant_id"]
    lista   = Protocolo.query.filter_by(tenant_id=tid).order_by(Protocolo.nome).all()
    insumos = Insumo.query.filter_by(tenant_id=tid).order_by(Insumo.nome).all()
    config  = Configuracao.query.filter_by(tenant_id=tid).first()
    _, hora_com_lucro = calcular_hora_clinica(config)

    protocolos_dados = []
    melhor_margem    = None
    mais_lucrativo   = None

    for p in lista:
        itens         = json.loads(p.itens_json or "[]")
        custo_insumos = sum(i.get("custo", 0) for i in itens)
        custo_hora    = (p.horas_clinica or 0) * hora_com_lucro
        custo_total   = custo_insumos + custo_hora
        pd = {"protocolo": p, "itens": itens,
              "custo_insumos": custo_insumos,
              "custo_hora": custo_hora,
              "custo_total": custo_total}
        protocolos_dados.append(pd)

        for preco in [p.preco1, p.preco2, p.preco3]:
            if preco and preco > 0 and custo_total > 0:
                lucro  = preco - custo_total
                margem = (lucro / preco * 100) if preco > 0 else 0
                if melhor_margem is None or margem > melhor_margem["margem"]:
                    melhor_margem = {"nome": p.nome, "margem": margem, "preco": preco}
                if mais_lucrativo is None or lucro > mais_lucrativo["lucro"]:
                    mais_lucrativo = {"nome": p.nome, "lucro": lucro, "preco": preco}

    return render_template("protocolos.html",
                           protocolos_dados=protocolos_dados,
                           insumos=insumos,
                           hora_com_lucro=hora_com_lucro,
                           melhor_margem=melhor_margem,
                           mais_lucrativo=mais_lucrativo)

@app.route("/protocolos/salvar", methods=["POST"])
@login_required
@tenant_ativo
def protocolo_salvar():
    tid   = session["tenant_id"]
    pid   = request.form.get("id")
    nome  = request.form.get("nome", "").strip()
    horas = float(request.form.get("horas_clinica", 0) or 0)
    p1    = float(request.form.get("preco1", 0) or 0)
    p2    = float(request.form.get("preco2", 0) or 0)
    p3    = float(request.form.get("preco3", 0) or 0)

    insumo_ids  = request.form.getlist("insumo_id")
    quantidades = request.form.getlist("quantidade")
    itens = []
    for iid, qtd in zip(insumo_ids, quantidades):
        if not iid:
            continue
        ins = db.session.get(Insumo, int(iid))
        if ins and ins.tenant_id == tid:
            q = float(qtd or 0)
            itens.append({"insumo_id": iid, "nome": ins.nome,
                          "quantidade": q, "custo": ins.custo_unitario * q})

    if pid:
        p = db.session.get(Protocolo, int(pid))
        if p and p.tenant_id == tid:
            p.nome = nome; p.horas_clinica = horas
            p.preco1 = p1; p.preco2 = p2; p.preco3 = p3
            p.itens_json = json.dumps(itens)
    else:
        db.session.add(Protocolo(tenant_id=tid, nome=nome, horas_clinica=horas,
                                 preco1=p1, preco2=p2, preco3=p3,
                                 itens_json=json.dumps(itens)))
    db.session.commit()
    return redirect(url_for("protocolos"))

@app.route("/protocolos/excluir/<int:id>")
@login_required
@tenant_ativo
def protocolo_excluir(id):
    tid = session["tenant_id"]
    p   = db.session.get(Protocolo, id)
    if p and p.tenant_id == tid:
        db.session.delete(p); db.session.commit()
    return redirect(url_for("protocolos"))

# ── Init ──────────────────────────────────────────────────────────────────────

def init_db():
    with app.app_context():
        db.create_all()
        try:
            if db_url.startswith("postgresql"):
                db.session.execute(db.text(
                    "ALTER TABLE usuario ADD COLUMN IF NOT EXISTS ultimo_acesso TIMESTAMP"))
            else:
                db.session.execute(db.text(
                    "ALTER TABLE usuario ADD COLUMN ultimo_acesso TIMESTAMP"))
            db.session.commit()
        except Exception:
            db.session.rollback()
        try:
            if db_url.startswith("postgresql"):
                db.session.execute(db.text(
                    "ALTER TABLE configuracao ADD COLUMN IF NOT EXISTS regime_tributario VARCHAR(50) DEFAULT ''"))
            else:
                db.session.execute(db.text(
                    "ALTER TABLE configuracao ADD COLUMN regime_tributario VARCHAR(50) DEFAULT ''"))
            db.session.commit()
        except Exception:
            db.session.rollback()
        if not Usuario.query.filter_by(is_super_admin=True).first():
            sa = Usuario(
                email=os.environ.get("SUPER_ADMIN_EMAIL", "admin@a01.com.br"),
                senha=generate_password_hash(os.environ.get("SUPER_ADMIN_SENHA", "a01admin2026")),
                nome="A'01 Negócios", is_admin=True, is_super_admin=True)
            db.session.add(sa)
            db.session.commit()

init_db()

if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5000))
    local = not os.environ.get("DATABASE_URL")
    if local:
        def abrir():
            import time; time.sleep(1.5)
            webbrowser.open(f"http://localhost:{port}")
        threading.Thread(target=abrir, daemon=True).start()
    app.run(host="0.0.0.0", port=port, debug=local)
