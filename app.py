from flask import Flask, render_template, request, redirect, url_for, session, send_file
from database import *
from export import gerar_excel
import os
import io

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or os.urandom(32)

ADMIN_EMAIL = "admin@a01.com.br"

init_db()

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'cliente_id' not in session:
            return redirect(url_for('login'))
        if session.get('is_admin'):
            return redirect(url_for('admin'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'cliente_id' not in session:
            return redirect(url_for('login'))
        if not session.get('is_admin'):
            return redirect(url_for('protocolos'))
        return f(*args, **kwargs)
    return decorated

# ─── AUTH ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if 'cliente_id' in session:
        return redirect(url_for('protocolos'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    erro = None
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        senha = request.form.get('senha', '')
        with get_db() as conn:
            cliente = conn.execute('SELECT * FROM clientes WHERE email=?', (email,)).fetchone()
        if cliente and verificar_senha(senha, cliente['senha_hash']):
            session['cliente_id'] = cliente['id']
            session['cliente_nome'] = cliente['nome']
            session['is_admin'] = (email == ADMIN_EMAIL)
            return redirect(url_for('admin') if session['is_admin'] else url_for('protocolos'))
        erro = 'E-mail ou senha incorretos.'
    return render_template('login.html', erro=erro)

@app.route('/cadastro', methods=['GET', 'POST'])
def cadastro():
    erro = None
    if request.method == 'POST':
        nome = request.form.get('nome', '').strip()
        email = request.form.get('email', '').strip().lower()
        senha = request.form.get('senha', '')
        if not nome or not email or not senha:
            erro = 'Preencha todos os campos.'
        elif len(senha) < 6:
            erro = 'A senha deve ter pelo menos 6 caracteres.'
        else:
            try:
                with get_db() as conn:
                    conn.execute('INSERT INTO clientes (nome, email, senha_hash) VALUES (?,?,?)',
                                 (nome, email, hash_senha(senha)))
                    cliente_id = conn.execute('SELECT id FROM clientes WHERE email=?', (email,)).fetchone()['id']
                    conn.execute('INSERT INTO configuracoes (cliente_id) VALUES (?)', (cliente_id,))
                    conn.execute('INSERT INTO custos_fixos (cliente_id, descricao, valor) VALUES (?,?,?)', (cliente_id, 'Aluguel', 0))
                    conn.execute('INSERT INTO custos_variaveis (cliente_id, descricao, valor) VALUES (?,?,?)', (cliente_id, 'Materiais de consumo', 0))
                    conn.execute('INSERT INTO insumos (cliente_id, nome, unidade, quantidade, custo_unitario) VALUES (?,?,?,?,?)',
                                 (cliente_id, '__hora_clinica__', 'unidade', 1, 0))
                session['cliente_id'] = cliente_id
                session['cliente_nome'] = nome
                session['is_admin'] = False
                return redirect(url_for('configuracoes'))
            except Exception:
                erro = 'Este e-mail já está cadastrado.'
    return render_template('cadastro.html', erro=erro)

@app.route('/sair')
def sair():
    session.clear()
    return redirect(url_for('login'))

@app.route('/alterar-senha', methods=['GET', 'POST'])
@login_required
def alterar_senha():
    erro = None
    sucesso = None
    if request.method == 'POST':
        atual = request.form.get('senha_atual', '')
        nova = request.form.get('senha_nova', '')
        confirma = request.form.get('senha_confirma', '')
        cid = session['cliente_id']
        with get_db() as conn:
            cliente = conn.execute('SELECT * FROM clientes WHERE id=?', (cid,)).fetchone()
        if not verificar_senha(atual, cliente['senha_hash']):
            erro = 'Senha atual incorreta.'
        elif len(nova) < 6:
            erro = 'A nova senha deve ter pelo menos 6 caracteres.'
        elif nova != confirma:
            erro = 'As senhas não coincidem.'
        else:
            with get_db() as conn:
                conn.execute('UPDATE clientes SET senha_hash=? WHERE id=?', (hash_senha(nova), cid))
            sucesso = 'Senha alterada com sucesso!'
    return render_template('alterar_senha.html', erro=erro, sucesso=sucesso)

# ─── CONFIGURAÇÕES ───────────────────────────────────────────────────────────

@app.route('/configuracoes')
@login_required
def configuracoes():
    cid = session['cliente_id']
    with get_db() as conn:
        cfg = conn.execute('SELECT * FROM configuracoes WHERE cliente_id=?', (cid,)).fetchone()
        fixos = conn.execute('SELECT * FROM custos_fixos WHERE cliente_id=? ORDER BY id', (cid,)).fetchall()
        variaveis = conn.execute('SELECT * FROM custos_variaveis WHERE cliente_id=? ORDER BY id', (cid,)).fetchall()
    hora = get_hora_clinica(cid)
    return render_template('configuracoes.html', cfg=cfg, fixos=fixos, variaveis=variaveis, hora=hora)

@app.route('/configuracoes/salvar', methods=['POST'])
@login_required
def salvar_configuracoes():
    cid = session['cliente_id']
    pro_labore = float(request.form.get('pro_labore', 0) or 0)
    lucro = float(request.form.get('lucro_desejado', 0) or 0)
    horas = float(request.form.get('horas_mes', 1) or 1)
    fixo_descs = request.form.getlist('fixo_desc')
    fixo_vals = request.form.getlist('fixo_val')
    var_descs = request.form.getlist('var_desc')
    var_vals = request.form.getlist('var_val')
    with get_db() as conn:
        conn.execute('UPDATE configuracoes SET pro_labore=?, lucro_desejado=?, horas_mes=? WHERE cliente_id=?',
                     (pro_labore, lucro, horas, cid))
        conn.execute('DELETE FROM custos_fixos WHERE cliente_id=?', (cid,))
        for d, v in zip(fixo_descs, fixo_vals):
            if d.strip():
                conn.execute('INSERT INTO custos_fixos (cliente_id, descricao, valor) VALUES (?,?,?)',
                             (cid, d.strip(), float(v or 0)))
        conn.execute('DELETE FROM custos_variaveis WHERE cliente_id=?', (cid,))
        for d, v in zip(var_descs, var_vals):
            if d.strip():
                conn.execute('INSERT INTO custos_variaveis (cliente_id, descricao, valor) VALUES (?,?,?)',
                             (cid, d.strip(), float(v or 0)))
    return redirect(url_for('configuracoes'))

# ─── INSUMOS ─────────────────────────────────────────────────────────────────

@app.route('/insumos')
@login_required
def insumos():
    cid = session['cliente_id']
    hora = get_hora_clinica(cid)
    with get_db() as conn:
        lista = conn.execute("SELECT * FROM insumos WHERE cliente_id=? AND nome != '__hora_clinica__' ORDER BY nome", (cid,)).fetchall()
    return render_template('insumos.html', insumos=lista, hora=hora)

@app.route('/insumos/salvar', methods=['POST'])
@login_required
def salvar_insumo():
    cid = session['cliente_id']
    insumo_id = request.form.get('id')
    nome = request.form.get('nome', '').strip()
    unidade = request.form.get('unidade', '').strip()
    quantidade = float(request.form.get('quantidade', 1) or 1)
    custo = float(request.form.get('custo_unitario', 0) or 0)
    if not nome:
        return redirect(url_for('insumos'))
    with get_db() as conn:
        if insumo_id:
            conn.execute('UPDATE insumos SET nome=?, unidade=?, quantidade=?, custo_unitario=? WHERE id=? AND cliente_id=?',
                         (nome, unidade, quantidade, custo, insumo_id, cid))
        else:
            conn.execute('INSERT INTO insumos (cliente_id, nome, unidade, quantidade, custo_unitario) VALUES (?,?,?,?,?)',
                         (cid, nome, unidade, quantidade, custo))
    return redirect(url_for('insumos'))

@app.route('/insumos/deletar/<int:insumo_id>', methods=['POST'])
@login_required
def deletar_insumo(insumo_id):
    cid = session['cliente_id']
    with get_db() as conn:
        conn.execute("DELETE FROM insumos WHERE id=? AND cliente_id=? AND nome != '__hora_clinica__'", (insumo_id, cid))
    return redirect(url_for('insumos'))

# ─── PROTOCOLOS ──────────────────────────────────────────────────────────────

@app.route('/protocolos')
@login_required
def protocolos():
    cid = session['cliente_id']
    hora = get_hora_clinica(cid)
    with get_db() as conn:
        lista = conn.execute('SELECT * FROM protocolos WHERE cliente_id=? ORDER BY nome', (cid,)).fetchall()
        resultado = []
        for p in lista:
            custo = get_custo_protocolo(p['id'], hora)
            count = conn.execute('SELECT COUNT(*) as c FROM protocolo_insumos WHERE protocolo_id=?', (p['id'],)).fetchone()['c']
            resultado.append({**dict(p), 'custo': custo, 'count': count})
    return render_template('protocolos.html', protocolos=resultado, hora=hora)

@app.route('/protocolos/novo', methods=['GET', 'POST'])
@login_required
def novo_protocolo():
    cid = session['cliente_id']
    hora = get_hora_clinica(cid)
    if request.method == 'POST':
        return _salvar_protocolo(cid, None)
    with get_db() as conn:
        insumos_lista = conn.execute("SELECT * FROM insumos WHERE cliente_id=? ORDER BY nome", (cid,)).fetchall()
    insumos_lista = [dict(i) for i in insumos_lista]
    hora_insumo = next((i for i in insumos_lista if i['nome'] == '__hora_clinica__'), None)
    insumos_lista = [i for i in insumos_lista if i['nome'] != '__hora_clinica__']
    return render_template('protocolo_detalhe.html', protocolo=None, insumos=insumos_lista,
                           hora_insumo=hora_insumo, hora=hora, itens=[])

@app.route('/protocolos/<int:pid>', methods=['GET', 'POST'])
@login_required
def editar_protocolo(pid):
    cid = session['cliente_id']
    hora = get_hora_clinica(cid)
    if request.method == 'POST':
        return _salvar_protocolo(cid, pid)
    with get_db() as conn:
        protocolo = conn.execute('SELECT * FROM protocolos WHERE id=? AND cliente_id=?', (pid, cid)).fetchone()
        if not protocolo:
            return redirect(url_for('protocolos'))
        protocolo = dict(protocolo)
        insumos_lista = conn.execute("SELECT * FROM insumos WHERE cliente_id=? ORDER BY nome", (cid,)).fetchall()
        itens = conn.execute('''
            SELECT pi.id, pi.insumo_id, pi.quantidade_usada, i.nome, i.custo_unitario, i.unidade
            FROM protocolo_insumos pi JOIN insumos i ON pi.insumo_id=i.id
            WHERE pi.protocolo_id=?
        ''', (pid,)).fetchall()
        itens = [dict(i) for i in itens]
    insumos_lista = [dict(i) for i in insumos_lista]
    hora_insumo = next((i for i in insumos_lista if i['nome'] == '__hora_clinica__'), None)
    insumos_lista = [i for i in insumos_lista if i['nome'] != '__hora_clinica__']
    custo = get_custo_protocolo(pid, hora)
    return render_template('protocolo_detalhe.html', protocolo=protocolo, insumos=insumos_lista,
                           hora_insumo=hora_insumo, hora=hora, itens=itens, custo=custo)

def _salvar_protocolo(cid, pid):
    nome = request.form.get('nome', '').strip()
    c1n = request.form.get('cenario1_nome', 'Pix / Dinheiro')
    c1p = float(request.form.get('cenario1_preco', 0) or 0)
    c2n = request.form.get('cenario2_nome', 'Cartão à vista')
    c2p = float(request.form.get('cenario2_preco', 0) or 0)
    c3n = request.form.get('cenario3_nome', 'Parcelado')
    c3p = float(request.form.get('cenario3_preco', 0) or 0)
    cartao_pct = float(request.form.get('cartao_imposto_pct', 0) or 0)
    insumo_ids = request.form.getlist('insumo_id')
    quantidades = request.form.getlist('quantidade_usada')
    with get_db() as conn:
        if pid:
            conn.execute('''UPDATE protocolos SET nome=?,cenario1_nome=?,cenario1_preco=?,
                cenario2_nome=?,cenario2_preco=?,cenario3_nome=?,cenario3_preco=?,cartao_imposto_pct=?
                WHERE id=? AND cliente_id=?''',
                (nome,c1n,c1p,c2n,c2p,c3n,c3p,cartao_pct,pid,cid))
            conn.execute('DELETE FROM protocolo_insumos WHERE protocolo_id=?', (pid,))
        else:
            cur = conn.execute('''INSERT INTO protocolos
                (cliente_id,nome,cenario1_nome,cenario1_preco,cenario2_nome,cenario2_preco,cenario3_nome,cenario3_preco,cartao_imposto_pct)
                VALUES (?,?,?,?,?,?,?,?,?)''',
                (cid,nome,c1n,c1p,c2n,c2p,c3n,c3p,cartao_pct))
            pid = cur.lastrowid
        for iid, qtd in zip(insumo_ids, quantidades):
            if iid:
                conn.execute('INSERT INTO protocolo_insumos (protocolo_id, insumo_id, quantidade_usada) VALUES (?,?,?)',
                             (pid, iid, float(qtd or 1)))
    return redirect(url_for('protocolos'))

@app.route('/protocolos/deletar/<int:pid>', methods=['POST'])
@login_required
def deletar_protocolo(pid):
    cid = session['cliente_id']
    with get_db() as conn:
        conn.execute('DELETE FROM protocolos WHERE id=? AND cliente_id=?', (pid, cid))
    return redirect(url_for('protocolos'))

# ─── EXPORTAR EXCEL ──────────────────────────────────────────────────────────

@app.route('/exportar')
@login_required
def exportar():
    cid = session['cliente_id']
    nome = session['cliente_nome']
    wb = gerar_excel(cid, nome, get_db, get_hora_clinica, get_custo_protocolo)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    from datetime import datetime
    fname = f"A01_Precificacao_{nome.replace(' ','_')}_{datetime.now().strftime('%Y%m%d')}.xlsx"
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

# ─── ADMIN ───────────────────────────────────────────────────────────────────

@app.route('/admin')
@admin_required
def admin():
    with get_db() as conn:
        clientes = conn.execute('''
            SELECT c.id, c.nome, c.email, c.criado_em,
                   (SELECT COUNT(*) FROM protocolos WHERE cliente_id=c.id) as n_protocolos,
                   (SELECT COUNT(*) FROM insumos WHERE cliente_id=c.id AND nome != '__hora_clinica__') as n_insumos
            FROM clientes c WHERE c.email != ? ORDER BY c.criado_em DESC
        ''', (ADMIN_EMAIL,)).fetchall()
    return render_template('admin.html', clientes=[dict(c) for c in clientes])

@app.route('/admin/exportar/<int:cid>')
@admin_required
def admin_exportar(cid):
    with get_db() as conn:
        cliente = conn.execute('SELECT nome FROM clientes WHERE id=?', (cid,)).fetchone()
    if not cliente:
        return redirect(url_for('admin'))
    nome = cliente['nome']
    wb = gerar_excel(cid, nome, get_db, get_hora_clinica, get_custo_protocolo)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    from datetime import datetime
    fname = f"A01_Precificacao_{nome.replace(' ','_')}_{datetime.now().strftime('%Y%m%d')}.xlsx"
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@app.route('/admin/resetar-senha/<int:cid>', methods=['POST'])
@admin_required
def admin_resetar_senha(cid):
    nova_senha = request.form.get('nova_senha', '')
    if len(nova_senha) < 6:
        return redirect(url_for('admin'))
    with get_db() as conn:
        conn.execute('UPDATE clientes SET senha_hash=? WHERE id=?', (hash_senha(nova_senha), cid))
    return redirect(url_for('admin'))

if __name__ == '__main__':
    app.run(debug=True)
