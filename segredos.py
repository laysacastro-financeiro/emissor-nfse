"""Segredos do sistema: nunca ficam no código nem no repositório.

Cada segredo vem de uma variável de ambiente ou, se ela não existir, de um valor aleatório gerado na primeira
vez e guardado em dados/ (pasta que não vai para o Git).
"""
from __future__ import annotations

import os
import secrets

import config


def segredo(nome: str, variavel_de_ambiente: str) -> str:
    valor = os.environ.get(variavel_de_ambiente, "").strip()
    if valor:
        return valor
    arquivo = config.DADOS_DIR / f"{nome}.txt"
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    if not arquivo.exists():
        arquivo.write_text(secrets.token_hex(32), "utf-8")
        try:
            arquivo.chmod(0o600)  # no Windows é ignorado; no Linux/Mac só o dono lê
        except OSError:
            pass
    return arquivo.read_text("utf-8").strip()
