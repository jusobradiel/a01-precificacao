import sqlite3
import bcrypt
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'a01.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS clientes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                senha_hash TEXT NOT NULL,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS configuracoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cliente_id INTEGER NOT NULL UNIQUE,
                pro_labore REAL DEFAULT 0,
                lucro_desejado REAL DEFAULT 0,
                horas_mes REAL DEFAULT 0,
                FOREIGN KEY (cliente_id) REFERENCES clientes(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS custos_fixos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cliente_id INTEGER NOT NULL,
                descricao TEXT NOT NULL,
                valor REAL DEFAULT 0,
                FOREIGN KEY (cliente_id) REFERENCES clientes(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS custos_variaveis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cliente_id INTEGER NOT NULL,
                descricao TEXT NOT NULL,
                valor REAL DEFAULT 0,
                FOREIGN KEY (cliente_id) REFERENCES clientes(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS insumos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cliente_id INTEGER NOT NULL,
                nome TEXT NOT NULL,
                unidade TEXT DEFAULT '',
                quantidade REAL DEFAULT 1,
                custo_unitario REAL DEFAULT 0,
                FOREIGN KEY (cliente_id) REFERENCES clientes(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS protocolos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cliente_id INTEGER NOT NULL,
                nome TEXT NOT NULL,
                cenario1_nome TEXT DEFAULT 'Pix / Dinheiro',
                cenario1_preco REAL DEFAULT 0,
                cenario2_nome TEXT DEFAULT 'Cartão à vista',
                cenario2_preco REAL DEFAULT 0,
                cenario3_nome TEXT DEFAULT 'Parcelado',
                cenario3_preco REAL DEFAULT 0,
                cartao_imposto_pct REAL DEFAULT 0,
                FOREIGN KEY (cliente_id) REFERENCES clientes(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS protocolo_insumos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                protocolo_id INTEGER NOT NULL,
                insumo_id INTEGER NOT NULL,
                quantidade_usada REAL DEFAULT 1,
                FOREIGN KEY (protocolo_id) REFERENCES protocolos(id) ON DELETE CASCADE,
                FOREIGN KEY (insumo_id) REFERENCES insumos(id) ON DELETE CASCADE
            );
        ''')

def hash_senha(senha):
    return bcrypt.hashpw(senha.encode(), bcrypt.gensalt()).decode()

def verificar_senha(senha, hash_):
    return bcrypt.checkpw(senha.encode(), hash_.encode())

def get_hora_clinica(cliente_id):
    with get_db() as conn:
        cfg = conn.execute('SELECT * FROM configuracoes WHERE cliente_id=?', (cliente_id,)).fetchone()
        if not cfg:
            return 0
        fixos = conn.execute('SELECT SUM(valor) as t FROM custos_fixos WHERE cliente_id=?', (cliente_id,)).fetchone()['t'] or 0
        variaveis = conn.execute('SELECT SUM(valor) as t FROM custos_variaveis WHERE cliente_id=?', (cliente_id,)).fetchone()['t'] or 0
        total = (fixos + variaveis + cfg['pro_labore']) * (1 + cfg['lucro_desejado'] / 100)
        horas = cfg['horas_mes'] or 1
        return round(total / horas, 2)

def get_custo_protocolo(protocolo_id, hora_clinica):
    with get_db() as conn:
        itens = conn.execute('''
            SELECT pi.quantidade_usada, i.custo_unitario, i.nome
            FROM protocolo_insumos pi
            JOIN insumos i ON pi.insumo_id = i.id
            WHERE pi.protocolo_id = ?
        ''', (protocolo_id,)).fetchall()
        total = 0
        for item in itens:
            custo = hora_clinica if item['nome'] == '__hora_clinica__' else item['custo_unitario']
            total += custo * item['quantidade_usada']
        return round(total, 2)
