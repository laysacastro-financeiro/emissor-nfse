"""Validação de CPF e CNPJ (inclusive o CNPJ alfanumérico, emitido a partir de julho/2026)."""
import re

PESOS_CNPJ_1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
PESOS_CNPJ_2 = [6] + PESOS_CNPJ_1


def so_digitos(valor: str) -> str:
    return re.sub(r"\D", "", valor or "")


def normalizar_documento(valor: str) -> str:
    """Remove pontuação e deixa letras maiúsculas (CNPJ alfanumérico)."""
    return re.sub(r"[^0-9A-Za-z]", "", valor or "").upper()


def cpf_valido(cpf: str) -> bool:
    cpf = so_digitos(cpf)
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
    for posicao in (9, 10):
        soma = sum(int(cpf[i]) * (posicao + 1 - i) for i in range(posicao))
        if (soma * 10) % 11 % 10 != int(cpf[posicao]):
            return False
    return True


def cnpj_valido(cnpj: str) -> bool:
    cnpj = normalizar_documento(cnpj)
    if not re.fullmatch(r"[0-9A-Z]{12}[0-9]{2}", cnpj) or cnpj == cnpj[0] * 14:
        return False
    # Regra da Receita: cada caractere vale (código ASCII - 48); para dígitos é o próprio número.
    valores = [ord(ch) - 48 for ch in cnpj]
    for pesos, posicao in ((PESOS_CNPJ_1, 12), (PESOS_CNPJ_2, 13)):
        resto = sum(v * p for v, p in zip(valores, pesos)) % 11
        if (0 if resto < 2 else 11 - resto) != valores[posicao]:
            return False
    return True
