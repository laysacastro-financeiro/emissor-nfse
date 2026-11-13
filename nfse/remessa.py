"""Leitura e validação do arquivo de remessa exportado do Gerência.

Layout (separador ';'):
  C;documento;IE;razão social;nome fantasia;logradouro;número;complemento;IBGE;UF;CEP;bairro
  V;pedido;documento
  I;pedido;cód. serviço;descrição;detalhe (período e boleto);quantidade;valor unitário
Um registro C pode ser seguido de vários pares V/I (cliente com mais de uma venda no dia).
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .documentos import cnpj_valido, cpf_valido, normalizar_documento, so_digitos

# Tamanhos máximos dos campos do tomador na DPS nacional.
LIMITES = {"razao_social": 300, "logradouro": 255, "numero": 60, "complemento": 156, "bairro": 60}
CAMPOS_POR_TIPO = {"C": 12, "V": 3, "I": 7}
RE_BOLETO = re.compile(r"\bIU\d+\b")
RE_PACOTE = re.compile(r"Periodo\s+(?:de\s+)?(\w+)", re.IGNORECASE)
CENTAVOS = Decimal("0.01")


def limpar(valor: str) -> str:
    """Remove espaços extras e descarta campos só com pontuação ('.....', '...')."""
    texto = " ".join((valor or "").split())
    return texto if any(ch.isalnum() for ch in texto) else ""


def decodificar(conteudo: bytes) -> str:
    try:
        return conteudo.decode("utf-8-sig")
    except UnicodeDecodeError:
        return conteudo.decode("cp1252", errors="replace")


def _decimal(texto: str) -> Decimal | None:
    texto = (texto or "").strip()
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    try:
        return Decimal(texto)
    except InvalidOperation:
        return None


@dataclass
class Cliente:
    documento: str
    inscricao_estadual: str
    razao_social: str
    nome_fantasia: str
    logradouro: str
    numero: str
    complemento: str
    cod_municipio: str
    uf: str
    cep: str
    bairro: str
    linha: int

    @property
    def tipo_documento(self) -> str:
        return {11: "CPF", 14: "CNPJ"}.get(len(self.documento), "")


@dataclass
class Venda:
    pedido: str
    cliente: Cliente
    linha: int
    cod_servico: str = ""
    descricao: str = ""
    detalhe: str = ""
    quantidade: Decimal = Decimal("0")
    valor_unitario: Decimal = Decimal("0")
    tem_item: bool = False
    erros: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)

    @property
    def valor_total(self) -> Decimal:
        return (self.quantidade * self.valor_unitario).quantize(CENTAVOS)

    @property
    def boleto(self) -> str:
        achado = RE_BOLETO.search(self.detalhe)
        return achado.group(0) if achado else ""

    @property
    def pacote(self) -> str:
        achado = RE_PACOTE.search(self.detalhe)
        return achado.group(1).lower() if achado else ""

    @property
    def pronta(self) -> bool:
        return not self.erros


@dataclass
class Remessa:
    nome_arquivo: str
    vendas: list[Venda]
    erros_arquivo: list[str]
    # Vendas de clientes que a empresa decidiu não emitir nota (ver app.aplicar_sem_nota): saem de `vendas`.
    sem_nota: list[Venda] = field(default_factory=list)

    @property
    def prontas(self) -> list[Venda]:
        return [v for v in self.vendas if v.pronta]

    @property
    def com_erro(self) -> list[Venda]:
        return [v for v in self.vendas if not v.pronta]

    @property
    def sem_pendencia(self) -> list[Venda]:
        return [v for v in self.vendas if v.pronta and not v.avisos]

    @property
    def com_atencao(self) -> list[Venda]:
        """Prontas para emitir, mas com algo que vale conferir."""
        return [v for v in self.vendas if v.pronta and v.avisos]

    @property
    def valor_total(self) -> Decimal:
        return sum((v.valor_total for v in self.vendas), Decimal("0.00"))

    @property
    def valor_prontas(self) -> Decimal:
        return sum((v.valor_total for v in self.prontas), Decimal("0.00"))

    def venda(self, pedido: str) -> Venda | None:
        return next((v for v in self.vendas if v.pedido == pedido), None)


def _cliente(campos: list[str], linha: int) -> Cliente:
    cep = so_digitos(campos[10])
    if len(cep) == 7:  # zero à esquerda perdido na exportação
        cep = "0" + cep
    return Cliente(
        documento=normalizar_documento(campos[1]),
        inscricao_estadual=limpar(campos[2]),
        razao_social=limpar(campos[3]),
        nome_fantasia=limpar(campos[4]),
        logradouro=limpar(campos[5]),
        numero=limpar(campos[6]),
        complemento=limpar(campos[7]),
        cod_municipio=so_digitos(campos[8]),
        uf=limpar(campos[9]).upper(),
        cep=cep,
        bairro=limpar(campos[11]),
        linha=linha,
    )


def ler_remessa(conteudo: bytes, nome_arquivo: str = "remessa.txt") -> Remessa:
    vendas: dict[str, Venda] = {}
    erros_arquivo: list[str] = []
    cliente: Cliente | None = None

    for n, linha in enumerate(decodificar(conteudo).splitlines(), start=1):
        if not linha.strip():
            continue
        campos = linha.split(";")
        tipo = campos[0].strip().upper()
        if tipo not in CAMPOS_POR_TIPO:
            erros_arquivo.append(f"Linha {n}: tipo de registro desconhecido '{campos[0][:10]}'")
            continue
        if len(campos) != CAMPOS_POR_TIPO[tipo]:
            erros_arquivo.append(
                f"Linha {n}: registro {tipo} com {len(campos)} campos (esperado {CAMPOS_POR_TIPO[tipo]})"
            )
            if tipo == "C":
                cliente = None
            continue

        if tipo == "C":
            cliente = _cliente(campos, n)
        elif tipo == "V":
            pedido = campos[1].strip()
            if cliente is None:
                erros_arquivo.append(f"Linha {n}: pedido {pedido} sem registro de cliente (C) antes")
                continue
            if pedido in vendas:
                vendas[pedido].erros.append(f"Pedido repetido no arquivo (linha {n})")
                continue
            venda = Venda(pedido=pedido, cliente=cliente, linha=n)
            if normalizar_documento(campos[2]) != cliente.documento:
                venda.erros.append("Documento do registro V diferente do cadastro do cliente")
            vendas[pedido] = venda
        else:  # I
            pedido = campos[1].strip()
            venda = vendas.get(pedido)
            if venda is None:
                erros_arquivo.append(f"Linha {n}: item do pedido {pedido} sem registro V")
                continue
            if venda.tem_item:
                venda.erros.append("Pedido com mais de um item — a nota aceita um serviço por pedido")
                continue
            venda.tem_item = True
            venda.cod_servico = campos[2].strip()
            venda.descricao = limpar(campos[3])
            venda.detalhe = limpar(campos[4])
            venda.quantidade = _decimal(campos[5]) or Decimal("0")
            venda.valor_unitario = _decimal(campos[6]) or Decimal("0")

    lista = list(vendas.values())
    vendas_por_cliente = Counter(id(v.cliente) for v in lista)
    for venda in lista:
        _validar(venda)
        qtd = vendas_por_cliente[id(venda.cliente)]
        if qtd > 1:
            venda.avisos.append(f"Cliente com {qtd} vendas neste arquivo — conferir cada nota com seu boleto")
    return Remessa(nome_arquivo=nome_arquivo, vendas=lista, erros_arquivo=erros_arquivo)


def extrair_vendas(conteudo: bytes, pedidos: list[str]) -> bytes:
    """Novo arquivo de remessa só com os pedidos escolhidos, copiando as linhas C/V/I originais."""
    escolhidos = set(pedidos)
    saida, cliente, cliente_escrito, pedido_atual = [], None, None, None
    for linha in decodificar(conteudo).splitlines():
        tipo = linha.split(";", 1)[0]
        if tipo == "C":
            cliente = linha
        elif tipo == "V":
            pedido_atual = linha.split(";")[1].strip()
            if pedido_atual in escolhidos:
                if cliente_escrito is not cliente:
                    saida.append(cliente)
                    cliente_escrito = cliente
                saida.append(linha)
        elif tipo == "I" and pedido_atual in escolhidos and linha.split(";")[1].strip() == pedido_atual:
            saida.append(linha)
    return ("\r\n".join(saida) + "\r\n").encode("utf-8")


CAMPOS_ENDERECO = ("logradouro", "numero", "complemento", "bairro")  # texto do endereço que a equipe pode corrigir


def validar_endereco(c: Cliente) -> tuple[list[str], list[str]]:
    """(erros, avisos) do texto do endereço do tomador (sem CEP/município)."""
    erros, avisos = [], []
    if not c.logradouro:
        erros.append("Logradouro não informado")
    if not c.bairro:
        erros.append("Bairro não informado")
    if not c.numero:
        avisos.append("Endereço sem número — será enviado 'S/N'")
    if c.logradouro and c.logradouro.upper() == c.bairro.upper():
        avisos.append("Logradouro igual ao bairro — conferir cadastro do cliente")
    for campo in CAMPOS_ENDERECO:
        if len(getattr(c, campo)) > LIMITES[campo]:
            avisos.append(f"Campo {campo} acima de {LIMITES[campo]} caracteres — será cortado")
    return erros, avisos


def _validar(venda: Venda) -> None:
    c = venda.cliente
    erros, avisos = venda.erros, venda.avisos

    if not c.documento:
        erros.append("Cliente sem CPF/CNPJ")
    elif c.tipo_documento == "CPF":
        if not cpf_valido(c.documento):
            erros.append(f"CPF inválido ({c.documento})")
    elif c.tipo_documento == "CNPJ":
        if not cnpj_valido(c.documento):
            erros.append(f"CNPJ inválido ({c.documento})")
    else:
        erros.append(f"Documento com {len(c.documento)} caracteres (esperado CPF 11 ou CNPJ 14)")

    if not c.razao_social:
        erros.append("Razão social não informada")
    if not re.fullmatch(r"\d{7}", c.cod_municipio):
        erros.append("Código IBGE do município inválido")
    if not re.fullmatch(r"[A-Z]{2}", c.uf):
        erros.append("UF inválida")
    if len(c.cep) != 8:
        erros.append("CEP inválido")
    erros_endereco, avisos_endereco = validar_endereco(c)
    erros.extend(erros_endereco)
    avisos.extend(avisos_endereco)
    if len(c.razao_social) > LIMITES["razao_social"]:
        avisos.append(f"Campo razao_social acima de {LIMITES['razao_social']} caracteres — será cortado")

    if not venda.tem_item:
        erros.append("Pedido sem item (registro I)")
        return
    if not venda.descricao:
        erros.append("Descrição do serviço vazia")
    if venda.quantidade <= 0:
        erros.append("Quantidade inválida")
    if venda.valor_unitario <= 0:
        erros.append("Valor inválido")
    # O código do item (13 em set/2026; 24 avulso, 25 mensal, 26 semestral, 27 anual em out/2026) é um
    # código interno do Gerência por pacote e não entra na nota — por isso não gera aviso.
    if not venda.pacote:
        avisos.append("Pacote (mensal/anual/semestral/avulso) não identificado na descrição")
    if not venda.boleto:
        avisos.append("Número do boleto (IU...) não encontrado na descrição")
