"""
Gerador de chave de ativacao - USO EXCLUSIVO DO VENDEDOR
Execute: python keygen.py
"""
from datetime import datetime
import pytz

def gerar_chave(ano=None, mes=None, dia=None, hora=None):
    if not all([ano, mes, dia, hora is not None]):
        brasilia = pytz.timezone('America/Sao_Paulo')
        agora = datetime.now(brasilia)
        ano  = agora.year
        mes  = agora.month
        dia  = agora.day
        hora = agora.hour

    chave = (ano * mes + dia * hora) * 10000
    return chave

if __name__ == "__main__":
    brasilia = pytz.timezone('America/Sao_Paulo')
    agora = datetime.now(brasilia)

    chave = gerar_chave()

    print("=" * 40)
    print(f"  Data/Hora Brasilia: {agora.strftime('%d/%m/%Y %Hh')}")
    print(f"  CHAVE: {chave}")
    print("=" * 40)
    print("Envie esse numero para o cliente via WhatsApp.")
    input("\nPressione Enter para fechar...")
