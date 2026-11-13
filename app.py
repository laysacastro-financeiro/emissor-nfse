"""Protótipo web do emissor de NFS-e.

Fluxo: importar remessa -> conferir pendências -> emitir no ISS Net -> conferência nota x pedido
-> baixar retorno para o Gerência.

Cada lote tem um ambiente. Lotes importados pela tela são de HOMOLOGAÇÃO (sem valor fiscal).
Produção só vale para lote com ambiente.txt = "producao", criado à parte e limitado a
config.LIMITE_NOTAS_PRODUCAO_POR_LOTE notas.
"""
from __future__ import annotations

import io
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
import time
import zipfile
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, send_file, url_for
from markupsafe import Markup

import config
from autenticacao import (
    auditar, proteger, registrar_rotas, segredo_sessao, somente_admin, ultimas_auditorias, usuario_logado as usuario_atual,
)
from nfse.assinatura import CertificadoA1
from nfse.cep import ResultadoCep, ServicoCep, formatar_cep
from nfse.historico_cep import HistoricoCep
from nfse.cnpj import ServicoCnpj
from nfse.conferencia import associar
from nfse.dps import FUSO_BRASILIA, montar_dps, valor_br
from nfse.emissao import (
    AUTORIZADA, PENDENTE, REJEITADA, ControleEnvios, buscar_documentos, diagnosticar_assinatura, dps_assinada, emitir_venda,
    testar_series,
)
from nfse.issnet import ARQUIVO_PERFIL, ClienteIssNet, ErroComunicacao, envelope_gerar_nfse
from nfse.nfse_xml import ler_nfse
from nfse.numeracao import RegistroDps
from nfse.remessa import CAMPOS_ENDERECO, LIMITES, Remessa, extrair_vendas, ler_remessa, limpar, validar_endereco
from nfse.retorno import RetornoNaoConfigurado, gerar_retorno

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
app.secret_key = segredo_sessao()
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = config.COOKIE_SEGURO  # True quando servido por HTTPS
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)  # o login expira
app.add_template_filter(lambda v: f"R$ {valor_br(v)}", "brl")


def _texto_para_js(valor) -> Markup:
    """Texto seguro para dentro de uma string JavaScript de um atributo HTML (ex.: onsubmit="confirm('...')"):
    aspas, barras, < > & e quebras de linha viram \\uXXXX, então um nome com aspas não vira código."""
    return Markup("".join(f"\\u{ord(c):04x}" if c in "\\'\"<>&`$" or ord(c) < 32 else c for c in str(valor)))


app.add_template_filter(_texto_para_js, "js")


@app.after_request
def _cabecalhos_de_seguranca(resposta):
    resposta.headers.setdefault("X-Content-Type-Options", "nosniff")
    resposta.headers.setdefault("X-Frame-Options", "DENY")  # ninguém põe o sistema dentro de outra página (clickjacking)
    resposta.headers.setdefault("Referrer-Policy", "no-referrer")
    resposta.headers.setdefault("Cache-Control", "no-store")  # páginas com dados de clientes não ficam em cache
    return resposta
proteger(app)  # login obrigatório + token anti-CSRF em todo POST
registrar_rotas(app)  # entrar, sair, primeiro acesso, usuários, minha senha

LOTES_DIR = config.DADOS_DIR / "lotes"
RE_LOTE = re.compile(r"^\d{8}-\d{6}$")
AMBIENTES = ("homologacao", "producao")
POR_PAGINA = 100
LIMITE_ENVIO_POR_CLIQUE = 50  # protótipo: o envio roda dentro da requisição; em produção vai para uma fila

# Numeração de DPS separada por ambiente e série: nota de teste não consome número da produção.
_registros: dict[tuple[str, str], RegistroDps] = {}
RE_SERIE = re.compile(r"^\d{1,5}$")
# Série do emissor web, usada pelo emissor anterior desde 01/10/2026: nunca compartilhar numeração.
# (A série 3 é a autorizada da empresa no ISS Net DF; os números livres dela vêm do ConsultarDpsDisponivel.)
SERIES_DE_OUTROS_EMISSORES = {"70001"}
PAGINAS_DPS_DISPONIVEIS = 80  # até 4.000 números (50 por página); para assim que houver o suficiente


def registro_de(ambiente: str, serie: str) -> RegistroDps:
    chave = (ambiente, serie)
    if chave not in _registros:
        _registros[chave] = RegistroDps(config.DADOS_DIR / f"numeracao_{ambiente}_serie_{serie}.json", serie)
    return _registros[chave]


def serie_do_lote(pasta: Path) -> str:
    """Série da DPS do lote: a de config, ou a confirmada pelo ISS Net em teste (serie.txt no lote)."""
    arquivo = pasta / "serie.txt"
    serie = arquivo.read_text("utf-8").strip() if arquivo.exists() else config.SERIE_DPS
    return serie if RE_SERIE.match(serie) else config.SERIE_DPS

# O certificado fica só na memória do servidor: ao reiniciar, é preciso carregar de novo.
sessao_certificado: dict = {"certificado": None, "clientes": {}}
trava_emissao = threading.Lock()


@app.context_processor
def dados_globais():
    return {"certificado": sessao_certificado["certificado"], "config": config}


def obter_cliente(ambiente: str) -> ClienteIssNet | None:
    certificado = sessao_certificado["certificado"]
    if certificado is None:
        flash("Carregue o certificado A1 antes de falar com o ISS Net.", "erro")
        return None
    clientes = sessao_certificado["clientes"]
    if ambiente not in clientes:
        clientes[ambiente] = ClienteIssNet(certificado, ambiente)
    return clientes[ambiente]


def perfil_confirmado() -> str:
    """Nome do perfil de assinatura já aceito pelo ISS Net (vale para os dois ambientes), ou ''."""
    if ARQUIVO_PERFIL.exists():
        return json.loads(ARQUIVO_PERFIL.read_text("utf-8")).get("homologacao", "")
    return ""


def ambiente_do_lote(pasta: Path) -> str:
    arquivo = pasta / "ambiente.txt"
    ambiente = arquivo.read_text("utf-8").strip() if arquivo.exists() else "homologacao"
    if ambiente not in AMBIENTES:
        abort(500, f"Ambiente inválido no lote: {ambiente}")
    return ambiente


def situacao(venda) -> str:
    return "erro" if venda.erros else ("atencao" if venda.avisos else "ok")


def motivo(mensagem: str) -> str:
    """Agrupa mensagens parecidas: 'Cliente com 2 vendas neste arquivo — ...' -> 'Cliente com N vendas neste arquivo'."""
    return re.sub(r"\d+", "N", re.split(r" — | \(", mensagem, maxsplit=1)[0])


def filtrar_vendas(vendas, situacao_escolhida: str, motivo_escolhido: str, busca: str):
    termo = busca.strip().lower()
    termo_documento = re.sub(r"[^0-9a-z]", "", termo)
    filtradas = []
    for venda in vendas:
        if situacao_escolhida != "todas" and situacao(venda) != situacao_escolhida:
            continue
        if motivo_escolhido and motivo_escolhido not in {motivo(m) for m in venda.erros + venda.avisos}:
            continue
        if termo:
            textos = (venda.pedido, venda.boleto, venda.cliente.razao_social, venda.cliente.nome_fantasia)
            achou_texto = any(termo in t.lower() for t in textos)
            achou_documento = bool(termo_documento) and termo_documento in venda.cliente.documento.lower()
            if not (achou_texto or achou_documento):
                continue
        filtradas.append(venda)
    return filtradas


def numeros_de_pagina(atual: int, total: int, vizinhas: int = 2) -> list[int | None]:
    """Números a mostrar no seletor de páginas: primeira, última e as próximas da atual; None = reticências."""
    mostrar = sorted({1, total} | set(range(max(1, atual - vizinhas), min(total, atual + vizinhas) + 1)))
    resultado: list[int | None] = []
    for n in mostrar:
        if resultado and n - resultado[-1] > 1:
            resultado.append(None if n - resultado[-1] > 2 else resultado[-1] + 1)
        resultado.append(n)
    return resultado


def pasta_lote(lote_id: str) -> Path:
    pasta = LOTES_DIR / lote_id
    if not RE_LOTE.match(lote_id) or not pasta.is_dir():
        abort(404)
    return pasta


def carregar(lote_id: str):
    pasta = pasta_lote(lote_id)
    nome = (pasta / "nome_original.txt").read_text("utf-8")
    remessa = ler_remessa((pasta / "remessa.txt").read_bytes(), nome)
    aplicar_endereco(remessa)
    aplicar_cep(remessa, pasta)
    aplicar_retencao(remessa, pasta)
    aplicar_sem_nota(remessa, pasta)
    return pasta, remessa, ambiente_do_lote(pasta)


def ler_json(arquivo: Path) -> dict:
    return json.loads(arquivo.read_text("utf-8")) if arquivo.exists() else {}


def gravar_json(arquivo: Path, dados: dict) -> None:
    temporario = arquivo.with_suffix(".tmp")
    temporario.write_text(json.dumps(dados, ensure_ascii=False, indent=1), "utf-8")
    temporario.replace(arquivo)


MOTIVO_CEP = "CEP não confere com o município"
MOTIVO_CEP_RECUSADO = "CEP recusado pelo ISS Net"
ARQUIVO_MEMORIA_CEP = "correcoes_clientes.json"  # em DADOS_DIR: vale para todas as remessas


def chave_cliente(documento: str, cep: str, cod_municipio: str) -> str:
    """Cadastro do cliente como veio do Gerência: o mesmo cliente com o mesmo CEP/município errado."""
    return f"{documento}|{cep}|{cod_municipio}"


def lembrar_correcao(venda, correcao: dict) -> None:
    """Guarda a correção feita pela equipe para o cliente: a próxima remessa em que o Gerência ainda mandar o
    mesmo CEP/município errado já sai corrigida, sem repetir o trabalho."""
    documento = venda.cliente.documento
    original_cod = correcao.get("de_municipio") or venda.cliente.cod_municipio
    if not documento or (correcao["cep"] == correcao["de"] and not correcao.get("cod_municipio")):
        return
    memoria = ler_json(config.DADOS_DIR / ARQUIVO_MEMORIA_CEP)
    memoria[chave_cliente(documento, correcao["de"], original_cod)] = {
        k: correcao[k] for k in ("cep", "cod_municipio", "uf", "cidade", "de_cidade", "de_uf", "usuario", "em")
        if k in correcao}
    config.DADOS_DIR.mkdir(parents=True, exist_ok=True)
    gravar_json(config.DADOS_DIR / ARQUIVO_MEMORIA_CEP, memoria)


ARQUIVO_CORRECAO_ENDERECO = "correcoes_endereco.json"  # em DADOS_DIR: CPF/CNPJ -> {de, para, usuario, em, ...}


def aplicar_endereco(remessa) -> None:
    """Sobrepõe à remessa o endereço (logradouro, número, complemento, bairro) corrigido pela equipe. A correção é
    do cliente (CPF/CNPJ) e vale para todas as remessas, mas só enquanto o Gerência ainda manda o texto original
    (`de`): se o cadastro for corrigido lá, ela deixa de ser aplicada. Refaz os erros/avisos do endereço."""
    correcoes = ler_json(config.DADOS_DIR / ARQUIVO_CORRECAO_ENDERECO)
    por_cliente: dict[int, list] = {}
    for venda in remessa.vendas:
        por_cliente.setdefault(id(venda.cliente), []).append(venda)
    for vendas in por_cliente.values():
        cliente = vendas[0].cliente
        erros_antes, avisos_antes = validar_endereco(cliente)
        correcao = correcoes.get(cliente.documento) if cliente.documento else None
        aplicados = []
        for campo, original in (correcao or {}).get("de", {}).items():
            if getattr(cliente, campo) == original:
                setattr(cliente, campo, correcao["para"][campo])
                aplicados.append(campo)
        erros_depois, avisos_depois = validar_endereco(cliente)
        for venda in vendas:
            venda.endereco_corrigivel = bool(erros_antes or avisos_antes or aplicados)
            if aplicados:
                venda.erros = [e for e in venda.erros if e not in erros_antes or e in erros_depois]
                venda.avisos = [a for a in venda.avisos if a not in avisos_antes or a in avisos_depois]
                venda.erros += [e for e in erros_depois if e not in erros_antes]
                venda.avisos += [a for a in avisos_depois if a not in avisos_antes]
                venda.avisos.append(f"Endereço corrigido ({', '.join(aplicados)}) por {correcao.get('usuario', '?')} — "
                                    "corrija também o cadastro no Gerência")


@app.post("/lote/<lote_id>/corrigir-endereco")
def corrigir_endereco_cadastro(lote_id):
    """Logradouro, número, complemento e bairro digitados pela equipe (ex.: 'Bairro não informado'). Fica gravado
    para o cliente: a próxima remessa em que o Gerência mandar o mesmo cadastro já sai corrigida."""
    pasta, remessa, _ = carregar(lote_id)
    pedido = request.form.get("pedido", "")
    venda = remessa.venda(pedido)
    novos = {campo: limpar(request.form.get(campo, "")) for campo in CAMPOS_ENDERECO}
    if not venda or not venda.cliente.documento:
        flash("Pedido sem CPF/CNPJ: o cadastro precisa ser corrigido no Gerência.", "erro")
    elif pedido_ja_emitido(pasta, pedido):
        flash("Pedido já emitido: o endereço não pode mais ser alterado.", "erro")
    elif not novos["logradouro"] or not novos["bairro"]:
        flash("Informe o logradouro e o bairro.", "erro")
    elif longos := [c for c in CAMPOS_ENDERECO if len(novos[c]) > LIMITES[c]]:
        flash(f"Texto grande demais em: {', '.join(longos)}.", "erro")
    else:
        documento, cliente = venda.cliente.documento, venda.cliente
        arquivo = config.DADOS_DIR / ARQUIVO_CORRECAO_ENDERECO
        todas = ler_json(arquivo)
        atual = todas.get(documento, {"de": {}, "para": {}})
        mudancas = []
        for campo in CAMPOS_ENDERECO:
            valor = getattr(cliente, campo)
            if novos[campo] == valor:
                continue
            # texto original do Gerência: o já guardado, se a correção ainda é a que está valendo
            original = atual["de"][campo] if campo in atual["de"] and atual["para"].get(campo) == valor else valor
            if novos[campo] == original:
                atual["de"].pop(campo, None), atual["para"].pop(campo, None)
            else:
                atual["de"][campo], atual["para"][campo] = original, novos[campo]
            mudancas.append(f"{campo}: '{valor}' -> '{novos[campo]}'")
        if not mudancas:
            flash("Nada mudou no endereço.", "erro")
        else:
            if atual["para"]:
                todas[documento] = {**atual, "cliente": cliente.razao_social, "pedido": pedido, "lote": lote_id,
                                    "usuario": (usuario_atual() or {}).get("login", "-"),
                                    "em": datetime.now(FUSO_BRASILIA).isoformat(timespec="seconds")}
            else:
                todas.pop(documento, None)
            config.DADOS_DIR.mkdir(parents=True, exist_ok=True)
            gravar_json(arquivo, todas)
            auditar(f"corrigiu o endereço do cliente {documento} (pedido {pedido}, lote {lote_id}): " + "; ".join(mudancas))
            flash(f"Pedido {pedido}: endereço corrigido. A correção vale para as próximas notas deste cliente. "
                  "Corrija também o cadastro no Gerência.", "ok")
    return redirect(url_for("lote", lote_id=lote_id) + "#vendas")


def aplicar_cep(remessa, pasta: Path) -> None:
    """Sobrepõe à remessa (1) os CEPs corrigidos pela equipe e (2) o resultado da verificação de CEP:
    CEP que não pertence ao município vira Erro (a nota seria recusada com E0240); CEP que o ViaCEP não
    conhece e para o qual não há sugestão vira Atenção."""
    correcoes = ler_json(pasta / "correcoes_cep.json")
    verificacoes = ler_json(pasta / "cep.json")
    memoria = ler_json(config.DADOS_DIR / ARQUIVO_MEMORIA_CEP)
    # Nota rejeitada por E0240 (o ISS Net tem a própria base de CEPs): precisa poder trocar o CEP mesmo "OK".
    recusadas = {p for p, e in ler_json(pasta / "envios.json").items()
                 if e.get("situacao") == REJEITADA and any(m.startswith("E0240") for m in e.get("mensagens", []))}
    for venda in remessa.vendas:
        venda.sugestoes_cep, venda.alternativa_municipio, venda.endereco_receita = [], {}, {}
        venda.cep_lembrada, venda.cep_recusado = False, False
        venda.cep_corrigivel = venda.pedido in correcoes or venda.pedido in recusadas  # mostra o campo de CEP manual
        lembrada = None if venda.pedido in correcoes else memoria.get(
            chave_cliente(venda.cliente.documento, venda.cliente.cep, venda.cliente.cod_municipio))
        if lembrada:
            cliente = venda.cliente
            antes = f"{formatar_cep(cliente.cep)}"
            cliente.cep = lembrada["cep"]
            if len(cliente.cep) == 8:
                venda.erros = [e for e in venda.erros if e != "CEP inválido"]
            if lembrada.get("cod_municipio"):
                cliente.cod_municipio, cliente.uf = lembrada["cod_municipio"], lembrada["uf"]
            venda.avisos.append(
                f"CEP corrigido para {formatar_cep(lembrada['cep'])} (era {antes}"
                + (f"; município {lembrada['cidade']}/{lembrada['uf']}" if lembrada.get("cod_municipio") else "")
                + f") — correção já feita por {lembrada.get('usuario', '?')} para este cliente")
            venda.cep_lembrada, venda.cep_corrigivel = True, True
            continue
        if venda.pedido in correcoes:
            corr = correcoes[venda.pedido]
            venda.cliente.cep = corr["cep"]
            if len(corr["cep"]) == 8:  # a remessa veio sem CEP/CEP malformado: o corrigido resolve esse erro
                venda.erros = [e for e in venda.erros if e != "CEP inválido"]
            if corr["cep"] != corr["de"]:
                venda.avisos.append(f"CEP corrigido para {formatar_cep(corr['cep'])} (era {formatar_cep(corr['de'])}) "
                                    f"por {corr['usuario']}")
            if corr.get("cod_municipio"):
                venda.cliente.cod_municipio, venda.cliente.uf = corr["cod_municipio"], corr["uf"]
                venda.avisos.append(f"Município do tomador corrigido para {corr['cidade']}/{corr['uf']} "
                                    f"(cadastro: {corr.get('de_cidade', '?')}/{corr.get('de_uf', '?')}) por {corr['usuario']}")
            achado = verificacoes.get(venda.pedido)
            if achado and achado["cep"] == venda.cliente.cep and achado["status"] == "geral":
                venda.sugestoes_cep = achado["sugestoes"]  # CEP geral mantido: segue oferecendo os da rua
            continue
        achado = verificacoes.get(venda.pedido)
        if not achado or achado["cep"] != venda.cliente.cep or achado["status"] in ("ok", "indisponivel"):
            continue
        venda.sugestoes_cep = achado["sugestoes"]
        venda.alternativa_municipio = achado.get("alternativa_municipio", {})
        venda.endereco_receita = achado.get("endereco_receita", {})
        venda.cep_corrigivel = True
        cep = formatar_cep(venda.cliente.cep)
        if achado["status"] == "geral":  # não bloqueia: o CEP geral é válido, mas a Receita indica a rua
            venda.avisos.append(f"CEP geral do município ({cep}) — há CEP da rua do endereço da Receita Federal: "
                                "veja as sugestões")
        elif achado["status"] == "divergente":
            venda.erros.append(f"{MOTIVO_CEP} — o CEP {cep} é de {achado['cidade_do_cep']}")
        elif achado["sugestoes"] or achado.get("ruas_pendentes"):
            # Com a busca de CEPs por rua ainda em andamento, o CEP inexistente já bloqueia (seria recusado: E0240).
            venda.erros.append(f"{MOTIVO_CEP} — o CEP {cep} não existe nos Correios"
                               + (", mas há CEP sugerido para o cliente" if achado["sugestoes"]
                                  else " (procurando CEPs sugeridos…)"))
        else:
            venda.avisos.append(f"CEP não confirmado — o CEP {cep} não foi encontrado nos Correios; confira o cadastro")
    avisar_cep_recusado_pelo_iss_net(remessa, pasta)


_historicos: dict[Path, HistoricoCep] = {}


def historico_cep() -> HistoricoCep:
    """CEPs que o ISS Net já aceitou ou recusou (lidos dos registros de comunicação, em dados/comunicacao)."""
    arquivo = config.DADOS_DIR / "historico_cep_issnet.json"
    if arquivo not in _historicos:
        _historicos[arquivo] = HistoricoCep(config.DADOS_DIR / "comunicacao", arquivo)
    return _historicos[arquivo]


def avisar_cep_recusado_pelo_iss_net(remessa, pasta: Path) -> None:
    """Venda cujo CEP (neste município) o ISS Net já recusou: avisa antes de emitir e oferece os CEPs que ele
    já aceitou no município. O CEP da rua continua sendo o ideal: os aceitos são alternativa se a rua falhar."""
    historico = historico_cep()
    autorizadas = {p for p, e in ler_json(pasta / "envios.json").items() if e.get("situacao") == AUTORIZADA}
    for venda in remessa.vendas:
        cliente = venda.cliente
        vezes = historico.recusado(cliente.cod_municipio, cliente.cep)
        if venda.pedido in autorizadas or not vezes:
            continue
        venda.cep_recusado = venda.cep_corrigivel = True
        venda.avisos.append(f"{MOTIVO_CEP_RECUSADO} — o CEP {formatar_cep(cliente.cep)} deu erro E0240 "
                            f"{vezes} vez{'es' if vezes > 1 else ''} neste município; troque antes de emitir")
        ja = {s["cep"] for s in venda.sugestoes_cep} | {cliente.cep}
        venda.sugestoes_cep += [
            {"cep": a["cep"], "logradouro": "", "bairro": "", "origem": "iss_aceito", "aceitos": a["aceitos"]}
            for a in historico.aceitos_do_municipio(cliente.cod_municipio) if a["cep"] not in ja]


ARQUIVO_ISS_RETIDO = "clientes_iss_retido.json"  # em DADOS_DIR: CNPJ/CPF -> quem marcou (vale para todas as remessas)


def aplicar_retencao(remessa, pasta: Path) -> None:
    """Tomador substituto tributário (EM059 no ISS Net): o cliente marcado sai com tpRetISSQN=2 em todas as notas
    novas. O valor da nota continua integral. `retencao_pendente` = rejeitada por EM059 e ainda não marcada."""
    retidos = ler_json(config.DADOS_DIR / ARQUIVO_ISS_RETIDO)
    envios = ler_json(pasta / "envios.json")
    rejeitadas = {p for p, e in envios.items()
                  if e.get("situacao") == REJEITADA and any(m.startswith("EM059") for m in e.get("mensagens", []))}
    autorizadas = {p for p, e in envios.items() if e.get("situacao") == AUTORIZADA}
    for venda in remessa.vendas:
        marca = retidos.get(venda.cliente.documento) if venda.cliente.documento else None
        venda.iss_retido = bool(marca)
        venda.retencao_pendente = venda.pedido in rejeitadas and not marca
        if marca and venda.pedido not in autorizadas:
            venda.avisos.append(
                f"ISS retido pelo tomador — a nota sai com retenção (tpRetISSQN=2) e valor integral de "
                f"R$ {venda.valor_total:.2f}; marcado por {marca.get('usuario', '?')}")


@app.post("/lote/<lote_id>/iss-retido")
def marcar_iss_retido(lote_id):
    """Marca (ou desfaz) o cliente do pedido como substituto tributário: as próximas notas dele saem com ISS retido."""
    pasta, remessa, _ = carregar(lote_id)
    pedido = request.form.get("pedido", "")
    venda = remessa.venda(pedido)
    desfazer = request.form.get("desfazer") == "1"
    if not venda or not venda.cliente.documento:
        flash("Pedido sem CPF/CNPJ: não dá para marcar a retenção.", "erro")
        return redirect(url_for("lote", lote_id=lote_id) + "#vendas")
    arquivo = config.DADOS_DIR / ARQUIVO_ISS_RETIDO
    retidos = ler_json(arquivo)
    documento = venda.cliente.documento
    if desfazer:
        retidos.pop(documento, None)
        auditar(f"desfez a marca de ISS retido do cliente {documento} ({venda.cliente.razao_social}), pedido {pedido}")
        flash(f"{venda.cliente.razao_social}: marca de ISS retido removida. As próximas notas saem sem retenção.", "ok")
    else:
        retidos[documento] = {"cliente": venda.cliente.razao_social, "pedido": pedido,
                              "usuario": (usuario_atual() or {}).get("login", "-"),
                              "em": datetime.now(FUSO_BRASILIA).isoformat(timespec="seconds")}
        auditar(f"marcou ISS retido pelo tomador para o cliente {documento} ({venda.cliente.razao_social}), pedido {pedido}")
        flash(f"{venda.cliente.razao_social}: ISS retido pelo tomador. As notas dele (inclusive o pedido {pedido}) "
              "saem com tpRetISSQN=2 e valor integral. Clique em Emitir para reenviar.", "ok")
    config.DADOS_DIR.mkdir(parents=True, exist_ok=True)
    gravar_json(arquivo, retidos)
    return redirect(url_for("lote", lote_id=lote_id) + "#vendas")


ARQUIVO_SEM_NOTA = "clientes_sem_nota.json"  # em DADOS_DIR: CNPJ/CPF -> quem decidiu (vale para todas as remessas)
MOTIVO_SEM_NOTA = "Sem inscrição municipal (EM030)"


def aplicar_sem_nota(remessa, pasta: Path) -> None:
    """Cliente que a empresa decidiu não emitir nota (EM030 no ISS Net: o CNPJ do tomador tem mais de uma inscrição
    municipal e não temos a certa): as vendas dele saem de `remessa.vendas` — logo de lotes, emissão e contagens — e
    vão para `remessa.sem_nota`. `sem_nota_pendente` = rejeitada por EM030 e ainda não decidida. Venda que já tem
    nota autorizada continua normal: nota emitida é registro fiscal."""
    marcados = ler_json(config.DADOS_DIR / ARQUIVO_SEM_NOTA)
    envios = ler_json(pasta / "envios.json")
    rejeitadas = {p for p, e in envios.items()
                  if e.get("situacao") == REJEITADA and any(m.startswith("EM030") for m in e.get("mensagens", []))}
    autorizadas = {p for p, e in envios.items() if e.get("situacao") == AUTORIZADA}
    mantidas, sem_nota = [], []
    for venda in remessa.vendas:
        marca = marcados.get(venda.cliente.documento) if venda.cliente.documento else None
        venda.sem_nota_pendente = venda.pedido in rejeitadas and not marca
        venda.sem_nota = marca if marca and venda.pedido not in autorizadas else None
        (sem_nota if venda.sem_nota else mantidas).append(venda)
    remessa.vendas, remessa.sem_nota = mantidas, sem_nota


def voltar_para(destino: str, padrao: str) -> str:
    """Endereço interno informado no formulário (nunca de outro site)."""
    return destino if destino.startswith("/") and not destino.startswith("//") else padrao


@app.post("/lote/<lote_id>/sem-nota")
def marcar_sem_nota(lote_id):
    """Pedido rejeitado por EM030: a empresa decide não emitir nota para este cliente. Grava o CPF/CNPJ numa lista
    própria (vale para todas as remessas); as vendas dele deixam de ir para o ISS Net e passam a aparecer em
    "Sem nota — decisão da empresa". Dá para desfazer."""
    pasta, remessa, _ = carregar(lote_id)
    pedido = request.form.get("pedido", "")
    venda = remessa.venda(pedido)
    if not venda or not venda.cliente.documento:
        flash("Pedido não encontrado ou sem CPF/CNPJ: não dá para marcar o cliente.", "erro")
    elif not getattr(venda, "sem_nota_pendente", False):
        flash("Só dá para marcar \"sem nota\" um pedido rejeitado pelo ISS Net com EM030.", "erro")
    else:
        arquivo = config.DADOS_DIR / ARQUIVO_SEM_NOTA
        marcados = ler_json(arquivo)
        documento = venda.cliente.documento
        marcados[documento] = {"cliente": venda.cliente.razao_social, "pedido": pedido, "motivo": MOTIVO_SEM_NOTA,
                               "usuario": (usuario_atual() or {}).get("login", "-"),
                               "em": datetime.now(FUSO_BRASILIA).isoformat(timespec="seconds")}
        config.DADOS_DIR.mkdir(parents=True, exist_ok=True)
        gravar_json(arquivo, marcados)
        auditar(f"decidiu não emitir nota para o cliente {documento} ({venda.cliente.razao_social}): "
                f"{MOTIVO_SEM_NOTA}, pedido {pedido}, lote {lote_id}")
        flash(f"{venda.cliente.razao_social}: sem nota por decisão da empresa. As vendas dele (inclusive o pedido "
              f"{pedido}) não vão mais para o ISS Net e ficam em \"Sem nota\".", "ok")
    return redirect(url_for("lote", lote_id=lote_id) + "#vendas")


@app.post("/sem-nota/desfazer")
def desfazer_sem_nota():
    """Tira o cliente da lista "sem nota": as vendas dele voltam para os lotes e para a emissão."""
    documento = re.sub(r"\D", "", request.form.get("documento", ""))
    arquivo = config.DADOS_DIR / ARQUIVO_SEM_NOTA
    marcados = ler_json(arquivo)
    marca = marcados.pop(documento, None)
    if marca:
        gravar_json(arquivo, marcados)
        auditar(f"voltou a emitir nota para o cliente {documento} ({marca.get('cliente', '?')}): marca de sem nota removida")
        flash(f"{marca.get('cliente', documento)}: voltou a ter nota. As vendas dele voltam para os lotes.", "ok")
    else:
        flash("Esse cliente não estava na lista de sem nota.", "erro")
    return redirect(voltar_para(request.form.get("voltar", ""), url_for("inicio")))


def notas_do_lote(pasta: Path):
    notas, erros = [], []
    for arquivo in sorted((pasta / "notas").glob("*.xml")):
        try:
            notas.extend(ler_nfse(arquivo.read_bytes()))
        except Exception as erro:  # arquivo corrompido ou que não é NFS-e
            erros.append(f"{arquivo.name}: {erro}")
    return notas, erros


def voltar_emissao(lote_id: str):
    return redirect(url_for("lote", lote_id=lote_id) + "#emissao")


LIXEIRA_DIR = config.DADOS_DIR / "lixeira"
_trava_id = threading.Lock()


def criar_pasta_lote() -> tuple[str, Path]:
    """Cria a pasta de um lote novo com id AAAAMMDD-HHMMSS livre (pula para o segundo seguinte se já existir,
    para duas pessoas criarem lotes no mesmo segundo sem erro)."""
    with _trava_id:
        momento = datetime.now(FUSO_BRASILIA)
        while True:
            lote_id = momento.strftime("%Y%m%d-%H%M%S")
            pasta = LOTES_DIR / lote_id
            if not pasta.exists() and not any(LIXEIRA_DIR.glob(f"{lote_id}_*")):
                pasta.mkdir(parents=True)
                return lote_id, pasta
            momento += timedelta(seconds=1)


def origem_do_lote(pasta: Path) -> str:
    """Id do lote importado de onde um lote de produção saiu ("" se não houver)."""
    arquivo = pasta / "origem.txt"
    texto = arquivo.read_text("utf-8").strip() if arquivo.exists() else ""
    return texto.removeprefix("lote ").strip()


def lotes_existentes() -> list[Path]:
    if not LOTES_DIR.exists():
        return []
    return sorted((p for p in LOTES_DIR.iterdir() if p.is_dir() and RE_LOTE.match(p.name)), reverse=True)


def filhos_de(lote_id: str) -> list[Path]:
    """Lotes de produção criados a partir de um lote importado."""
    return [p for p in lotes_existentes() if ambiente_do_lote(p) == "producao" and origem_do_lote(p) == lote_id]


def ja_retornadas(pasta: Path) -> set[str]:
    """Números de NFS-e do lote que já foram em algum arquivo de retorno para o Gerência."""
    arquivo = pasta / "retornos.json"
    historico = json.loads(arquivo.read_text("utf-8")) if arquivo.exists() else []
    return {numero for item in historico for numero in item.get("notas", [])}


def historico_retornos(pasta: Path) -> list[dict]:
    arquivo = pasta / "retornos.json"
    return json.loads(arquivo.read_text("utf-8")) if arquivo.exists() else []


def registrar_retorno(pasta: Path, nome: str, numeros: list[str], modo: str) -> None:
    historico = historico_retornos(pasta)
    historico.append({
        "arquivo": nome, "notas": numeros, "modo": modo,
        "gerado_em": datetime.now(FUSO_BRASILIA).isoformat(timespec="seconds"),
        "usuario": (usuario_atual() or {}).get("login", "-"),
    })
    (pasta / "retornos.json").write_text(json.dumps(historico, ensure_ascii=False, indent=1), "utf-8")


def resumo_lote(pasta: Path) -> dict:
    envios = ControleEnvios(pasta).envios.values()
    situacoes = Counter(e.situacao for e in envios)
    notas = len(list((pasta / "notas").glob("*.xml")))
    autorizadas = {e.pedido for e in ControleEnvios(pasta).envios.values() if e.situacao == AUTORIZADA}
    sem_nota = [p for p in sem_nota_do_lote(pasta) if p not in autorizadas]
    return {
        "id": pasta.name,
        "arquivo": (pasta / "nome_original.txt").read_text("utf-8"),
        "ambiente": ambiente_do_lote(pasta),
        "origem": origem_do_lote(pasta),
        "vendas": len(vendas_do_lote(pasta)) - len(sem_nota),
        "autorizadas": situacoes.get(AUTORIZADA, 0),
        "pendentes": situacoes.get(PENDENTE, 0),
        "retornadas": len(ja_retornadas(pasta)),
        # Nota emitida é registro fiscal: lote com nota (ou envio sem resposta) não pode ser excluído.
        "pode_excluir": notas == 0 and not situacoes.get(AUTORIZADA) and not situacoes.get(PENDENTE),
    }


ARQUIVO_ARQUIVADA = "arquivada.json"  # remessa/lote que a equipe tirou da lista de pendências
_cache_vendas: dict[Path, tuple[tuple, list[tuple[str, bool]], dict, dict]] = {}


def _dados_do_lote(pasta: Path) -> tuple[list[tuple[str, bool]], dict, dict]:
    """(vendas, valores, sem_nota) do lote; relido só quando a remessa, as correções de endereço ou a lista de
    clientes sem nota mudam."""
    arquivo = pasta / "remessa.txt"
    correcoes = config.DADOS_DIR / ARQUIVO_CORRECAO_ENDERECO
    marcas = config.DADOS_DIR / ARQUIVO_SEM_NOTA
    versao = (arquivo.stat().st_mtime_ns, correcoes.stat().st_mtime_ns if correcoes.exists() else 0,
              marcas.stat().st_mtime_ns if marcas.exists() else 0)
    if _cache_vendas.get(pasta, (None,))[0] != versao:
        remessa = ler_remessa(arquivo.read_bytes())
        aplicar_endereco(remessa)
        marcados = ler_json(marcas)
        sem_nota = {
            v.pedido: {"pedido": v.pedido, "boleto": v.boleto, "cliente": v.cliente.razao_social,
                       "documento": v.cliente.documento, "uf": v.cliente.uf, "pacote": v.pacote,
                       "valor": v.valor_total, "motivo": marcados[v.cliente.documento].get("motivo", MOTIVO_SEM_NOTA),
                       "usuario": marcados[v.cliente.documento].get("usuario", "-"),
                       "em": marcados[v.cliente.documento].get("em", "")}
            for v in remessa.vendas if v.cliente.documento and v.cliente.documento in marcados}
        _cache_vendas[pasta] = (versao, [(v.pedido, v.pronta) for v in remessa.vendas],
                                {v.pedido: v.valor_total for v in remessa.vendas}, sem_nota)
    _, vendas, valores, sem_nota = _cache_vendas[pasta]
    return vendas, valores, sem_nota


def vendas_do_lote(pasta: Path) -> list[tuple[str, bool]]:
    """(pedido, sem erro de cadastro) de cada venda do lote (inclusive as de clientes sem nota)."""
    return _dados_do_lote(pasta)[0]


def valores_do_lote(pasta: Path) -> dict[str, Decimal]:
    """Valor (R$) de cada pedido do lote, como veio do Gerência."""
    return _dados_do_lote(pasta)[1]


def sem_nota_do_lote(pasta: Path) -> dict[str, dict]:
    """Pedidos do lote cujo cliente está marcado como "sem nota" (a venda com nota autorizada é decidida pelo chamador)."""
    return _dados_do_lote(pasta)[2]


def data_do_lote(lote_id: str) -> str:
    """'20261030-082740' -> '30/10/2026 08:27'."""
    return f"{lote_id[6:8]}/{lote_id[4:6]}/{lote_id[:4]} {lote_id[9:11]}:{lote_id[11:13]}"


def painel_remessas(de: str = "", ate: str = "") -> dict:
    """Situação de cada remessa importada (e de lote de produção avulso): quantas notas foram emitidas, quantas já
    foram em arquivo de retorno para o Gerência e o que ainda impede a remessa de estar completa.

    `de` / `ate` (AAAA-MM-DD, opcionais): só entram as remessas importadas nesse período (data do id do lote).
    Vendas de clientes "sem nota" (decisão da empresa) não contam como erro nem como pendência: ficam à parte,
    em `sem_nota`."""
    lotes = lotes_existentes()
    producao = [p for p in lotes if ambiente_do_lote(p) == "producao"]
    notas: dict[str, dict] = {}  # pedido -> nota autorizada e se o retorno dela já foi gerado
    nao_autorizadas: dict[str, str] = {}  # pedido -> 'rejeitada' | 'pendente' (enquanto sem nota em nenhum lote)
    lote_do_problema: dict[str, str] = {}
    em_lote: set[str] = set()
    for pasta in producao:
        em_lote |= {pedido for pedido, _ in vendas_do_lote(pasta)}
        retornadas = ja_retornadas(pasta)
        for envio in ControleEnvios(pasta).envios.values():
            if envio.situacao == AUTORIZADA:
                notas[envio.pedido] = {"retornada": envio.numero_nfse in retornadas}
            elif envio.situacao in (REJEITADA, PENDENTE):
                nao_autorizadas[envio.pedido] = envio.situacao
                lote_do_problema[envio.pedido] = pasta.name
    grupos = [(p, filhos_de(p.name), False) for p in lotes if ambiente_do_lote(p) != "producao"]
    grupos += [(p, [p], True) for p in producao if not origem_do_lote(p)]  # lote de produção sem remessa (ex.: piloto)
    linhas = []
    lista_sem_nota: dict[str, dict] = {}
    for pasta, filhos, avulso in grupos:
        dia = pasta.name[:8]
        if (de and dia < de.replace("-", "")) or (ate and dia > ate.replace("-", "")):
            continue
        todos = vendas_do_lote(pasta)
        if not todos:
            continue
        # Cliente marcado "sem nota" (venda sem nota autorizada): fora das contagens e dos erros.
        isentas = {p: info for p, info in sem_nota_do_lote(pasta).items() if p not in notas}
        pedidos = [(p, ok) for p, ok in todos if p not in isentas]
        for p, info in isentas.items():
            lista_sem_nota.setdefault(p, {**info, "remessa": pasta.name, "arquivo": (pasta / "nome_original.txt").read_text("utf-8"),
                                          "data": data_do_lote(pasta.name)})
        valores = valores_do_lote(pasta)
        emitidas = [p for p, _ in pedidos if p in notas]
        total = len(pedidos)
        sem_nota = [(p, ok) for p, ok in pedidos if p not in notas]
        linha = {
            "id": pasta.name, "data": data_do_lote(pasta.name), "arquivo": (pasta / "nome_original.txt").read_text("utf-8"),
            "avulso": avulso, "lotes": [f.name for f in filhos], "vendas": total, "emitidas": len(emitidas),
            "valor_emitido": sum((valores.get(p, Decimal("0")) for p in emitidas), Decimal("0.00")),
            "sem_nota": len(isentas),
            "retornadas": sum(notas[p]["retornada"] for p in emitidas),
            "rejeitadas": sum(nao_autorizadas.get(p) == REJEITADA for p, _ in sem_nota),
            "pendentes": sum(nao_autorizadas.get(p) == PENDENTE for p, _ in sem_nota),
            "bloqueadas": sum(1 for p, ok in sem_nota if not ok and p not in em_lote),
            "sem_lote": sum(1 for p, ok in sem_nota if ok and p not in em_lote),
            "no_lote": sum(1 for p, _ in sem_nota if p in em_lote and p not in nao_autorizadas),
            "lotes_com_problema": sorted({lote_do_problema[p] for p, _ in sem_nota if p in nao_autorizadas}),
            "arquivada": ler_json(pasta / ARQUIVO_ARQUIVADA),
        }
        linha["sem_retorno"] = linha["emitidas"] - linha["retornadas"]
        erros = linha["rejeitadas"] + linha["pendentes"] + linha["bloqueadas"]
        andamento = linha["sem_lote"] + linha["no_lote"]
        linha["situacao"] = ("erro" if erros else "emitindo" if andamento else
                             "retorno" if linha["sem_retorno"] else "completa")
        if not linha["emitidas"] and not erros and total:
            linha["situacao"] = "nova"
        linhas.append(linha)
    arquivadas = [l for l in linhas if l["arquivada"]]
    linhas = [l for l in linhas if not l["arquivada"]]
    resumo = {
        "remessas": len(linhas), "completas": sum(l["situacao"] == "completa" for l in linhas),
        "sem_retorno": sum(l["sem_retorno"] for l in linhas),
        "rejeitadas": sum(l["rejeitadas"] + l["pendentes"] for l in linhas),
        "bloqueadas": sum(l["bloqueadas"] for l in linhas),
        "a_emitir": sum(l["sem_lote"] + l["no_lote"] for l in linhas),
        "emitidas": sum(l["emitidas"] for l in linhas),
        "valor_emitido": sum((l["valor_emitido"] for l in linhas), Decimal("0.00")),
        "sem_nota": sum(l["sem_nota"] for l in linhas),
    }
    return {"resumo": resumo, "atencao": [l for l in linhas if l["situacao"] != "completa"],
            "completas": [l for l in linhas if l["situacao"] == "completa"], "arquivadas": arquivadas,
            "sem_nota": sorted(lista_sem_nota.values(), key=lambda s: (s["remessa"], s["pedido"]))}


def periodo_da_requisicao() -> tuple[str, str]:
    """Período do filtro do início (?de=AAAA-MM-DD&ate=AAAA-MM-DD); data inválida é ignorada e datas invertidas
    são trocadas."""
    def data(nome: str) -> str:
        try:
            return date.fromisoformat(request.args.get(nome, "").strip()).isoformat()
        except ValueError:
            return ""
    de, ate = data("de"), data("ate")
    if de and ate and de > ate:
        de, ate = ate, de
    return de, ate


@app.get("/")
def inicio():
    de, ate = periodo_da_requisicao()
    hoje = datetime.now(FUSO_BRASILIA).date()
    atalhos = [("Hoje", hoje, hoje), ("Últimos 7 dias", hoje - timedelta(days=6), hoje),
               ("Este mês", hoje.replace(day=1), hoje)]
    return render_template("inicio.html", lotes=[resumo_lote(p) for p in lotes_existentes()],
                           painel=painel_remessas(de, ate), periodo={"de": de, "ate": ate},
                           atalhos=[(nome, a.isoformat(), b.isoformat()) for nome, a, b in atalhos])


def csv_seguro(texto) -> str:
    """Campo de CSV: sem ';' nem quebra de linha, e sem começar com = + - @ (o Excel trataria como fórmula)."""
    texto = str(texto).replace(";", ",").replace("\r", " ").replace("\n", " ")
    return "'" + texto if texto[:1] in ("=", "+", "-", "@") else texto


@app.get("/sem-nota.csv")
def sem_nota_csv():
    """Pedidos sem nota por decisão da empresa (clientes marcados após o EM030), para o financeiro acompanhar ou
    avisar o Gerência. Aceita o mesmo período do início (?de=&ate=)."""
    de, ate = periodo_da_requisicao()
    linhas = ["pedido;boleto;cliente;cnpj_cpf;uf;pacote;valor;remessa;data_da_remessa;motivo;decidido_por;decidido_em"]
    for s in painel_remessas(de, ate)["sem_nota"]:
        linhas.append(";".join(csv_seguro(c) for c in (
            s["pedido"], s["boleto"], s["cliente"], s["documento"], s["uf"], s["pacote"], valor_br(s["valor"]),
            s["arquivo"], s["data"], s["motivo"], s["usuario"], s["em"][:16].replace("T", " "))))
    conteudo = ("﻿" + "\r\n".join(linhas) + "\r\n").encode("utf-8")  # BOM: o Excel abre com acentos
    return send_file(io.BytesIO(conteudo), mimetype="text/csv", as_attachment=True,
                     download_name=f"pedidos_sem_nota_{datetime.now(FUSO_BRASILIA):%Y%m%d}.csv")


@app.post("/lote/<lote_id>/arquivar")
def arquivar_remessa(lote_id):
    """Tira a remessa (ou o lote de produção avulso, como o piloto) da lista de pendências do início. Não apaga nada:
    o registro fica no lote e pode ser desfeito."""
    pasta = pasta_lote(lote_id)
    motivo_arquivo = request.form.get("motivo", "").strip() or "arquivada pela equipe"
    gravar_json(pasta / ARQUIVO_ARQUIVADA, {
        "usuario": (usuario_atual() or {}).get("login", "-"), "motivo": motivo_arquivo,
        "em": datetime.now(FUSO_BRASILIA).isoformat(timespec="seconds")})
    auditar(f"arquivou a remessa/lote {lote_id}: {motivo_arquivo}")
    flash(f"{lote_id} arquivada: não aparece mais entre as pendências (continua em 'Arquivadas').", "ok")
    return redirect(url_for("inicio"))


@app.post("/lote/<lote_id>/desarquivar")
def desarquivar_remessa(lote_id):
    pasta = pasta_lote(lote_id)
    (pasta / ARQUIVO_ARQUIVADA).unlink(missing_ok=True)
    auditar(f"desarquivou a remessa/lote {lote_id}")
    flash(f"{lote_id} voltou para a lista de pendências.", "ok")
    return redirect(url_for("inicio"))


@app.post("/lote/<lote_id>/excluir")
def excluir_lote(lote_id):
    """Tira o lote da lista (vai para a lixeira em dados/lixeira, recuperável). Bloqueado se houver nota."""
    pasta = pasta_lote(lote_id)
    resumo = resumo_lote(pasta)
    if not resumo["pode_excluir"]:
        flash(f"O lote {lote_id} tem notas emitidas (ou envio sem resposta) e não pode ser excluído.", "erro")
        return redirect(url_for("inicio"))
    LIXEIRA_DIR.mkdir(parents=True, exist_ok=True)
    pasta.rename(LIXEIRA_DIR / f"{lote_id}_excluido_{datetime.now(FUSO_BRASILIA):%Y%m%d%H%M%S}")
    auditar(f"excluiu o lote {lote_id} ({resumo['arquivo']}, {resumo['vendas']} vendas) — movido para a lixeira")
    flash(f"Lote {lote_id} ({resumo['arquivo']}) excluído. Ele foi para a lixeira e pode ser recuperado se preciso.", "ok")
    return redirect(url_for("inicio"))


APLICAR_CEP_DA_RECEITA_AUTOMATICAMENTE = getattr(config, "APLICAR_CEP_DA_RECEITA_AUTOMATICAMENTE", True)
THREADS_VERIFICACAO_CEP = 6  # vendas conferidas ao mesmo tempo
BLOCO_VERIFICACAO_CEP = 24  # vendas por rodada paralela (o andamento é gravado a cada bloco)
ORCAMENTO_VERIFICACAO_CEP = 240  # segundos por clique; o botão pode ser repetido até terminar
servico_cep = ServicoCep(cnpj=ServicoCnpj())


def _falta_consultar_receita(anterior: dict, cliente) -> bool:
    """CEP com problema verificado antes de existir a consulta ao cadastro do CNPJ na Receita (ou com a Receita
    fora do ar na ocasião): consulta agora."""
    if "cep_geral" not in anterior:  # verificado antes de existir a conferência de CEP geral do município
        return True
    precisa = anterior["status"] in ("divergente", "nao_encontrado") or anterior["cep_geral"]
    return (precisa and not anterior.get("receita_consultada")
            and len(re.sub(r"\D", "", cliente.documento or "")) == 14)


def vendas_a_verificar(pasta: Path, remessa, ambiente: str):
    """Vendas ainda não emitidas (e, na remessa importada, ainda fora de qualquer lote de produção)."""
    if ambiente == "producao":
        envios = ControleEnvios(pasta).envios
        return [v for v in remessa.vendas if getattr(envios.get(v.pedido), "situacao", "") != AUTORIZADA]
    em_producao = pedidos_ja_em_producao()
    return [v for v in remessa.vendas if v.pedido not in em_producao]


trava_verificacao = threading.RLock()  # a verificação em segundo plano e o clique do usuário não gravam juntos
_verificacoes_em_andamento: dict[str, threading.Thread] = {}


def verificar_lista(pasta: Path, vendas, orcamento: float = ORCAMENTO_VERIFICACAO_CEP) -> tuple[int, int, int, int]:
    with trava_verificacao:
        return _verificar_lista(pasta, vendas, orcamento)


def verificando_ceps(lote_id: str) -> bool:
    fio = _verificacoes_em_andamento.get(lote_id)
    return bool(fio and fio.is_alive())


def verificar_ceps_em_segundo_plano(lote_id: str) -> None:
    """Confere o CEP de todas as vendas do lote logo após a importação, sem o usuário precisar clicar: quando
    ele abrir a lista, os problemas de CEP já estão sinalizados (e os do mesmo cliente já corrigidos antes vêm
    corrigidos). Roda em blocos de 60 s para não travar quem clica em Verificar CEPs ao mesmo tempo."""
    if verificando_ceps(lote_id):
        return

    def trabalho():
        try:
            faltavam = None
            for _ in range(300):
                pasta, remessa, ambiente = carregar(lote_id)
                vendas = vendas_a_verificar(pasta, remessa, ambiente)
                novas, _, restantes, _ = verificar_lista(pasta, vendas, 60)  # etapa rápida: status de todas
                if restantes:
                    if novas:
                        continue
                    break
                faltam = completar_sugestoes_de_rua(pasta, vendas, 60)  # etapa lenta: CEPs por nome de rua
                if not faltam or faltam == faltavam:
                    break
                faltavam = faltam
        except Exception as erro:  # a verificação é um auxílio: o botão manual continua valendo
            print(f"Verificação de CEPs em segundo plano do lote {lote_id} parou: {erro}")

    fio = threading.Thread(target=trabalho, name=f"ceps-{lote_id}", daemon=True)
    _verificacoes_em_andamento[lote_id] = fio
    fio.start()


def _verificar_lista(pasta: Path, vendas, orcamento: float) -> tuple[int, int, int, int]:
    """Verifica o CEP das vendas ainda não verificadas (ou com CEP alterado) e grava em cep.json do lote.

    Devolve (consultadas, com_problema, sem_tempo, indisponiveis). Vendas com correção da equipe são puladas.
    `sem_tempo` > 0 = o orçamento de tempo acabou: repetir a chamada continua de onde parou."""
    arquivo = pasta / "cep.json"
    feitas = ler_json(arquivo)
    correcoes = ler_json(pasta / "correcoes_cep.json")
    inicio = time.monotonic()
    novas = problemas = restantes = 0
    corrigidas = False
    a_fazer = []
    for venda in vendas:
        cliente = venda.cliente
        anterior = feitas.get(venda.pedido)
        corrigida = correcoes.get(venda.pedido)
        if getattr(venda, "cep_lembrada", False):
            continue  # cliente já corrigido em outra remessa: não gasta consulta
        if corrigida and corrigida["cep"] != corrigida["de"]:
            continue  # a equipe já trocou o CEP; só o município corrigido (CEP igual) segue sendo conferido
        if anterior and anterior["cep"] == cliente.cep and anterior["status"] != "indisponivel" \
                and not _falta_consultar_receita(anterior, cliente):
            continue
        a_fazer.append((venda, corrigida))

    def conferir(item, buscar_ruas=False):
        cliente = item[0].cliente
        try:
            return servico_cep.verificar(cliente.cep, cliente.cod_municipio, cliente.uf, cliente.logradouro,
                                         cliente.bairro, cliente.documento, buscar_ruas=buscar_ruas)
        except Exception:  # um cadastro estranho não derruba a remessa inteira: fica para a próxima rodada
            return ResultadoCep("indisponivel")

    # Etapa rápida: os CEPs distintos são consultados todos de uma vez, em paralelo, e cada venda sai com o status
    # (ok / com problema) e as sugestões da Receita. A busca de CEPs por nome de rua (ViaCEP, lenta) fica para a
    # etapa seguinte (completar_sugestoes_de_rua), que não atrasa a lista de problemas.
    servico_cep.pre_carregar([v.cliente.cep for v, _ in a_fazer], limite_segundos=max(5.0, orcamento / 2))
    with ThreadPoolExecutor(THREADS_VERIFICACAO_CEP) as pool:
        for i in range(0, len(a_fazer), BLOCO_VERIFICACAO_CEP):
            if time.monotonic() - inicio >= orcamento:
                restantes += len(a_fazer) - i
                break
            bloco = a_fazer[i : i + BLOCO_VERIFICACAO_CEP]
            for (venda, corrigida), resultado in zip(bloco, pool.map(conferir, bloco)):
                cliente = venda.cliente
                feitas[venda.pedido] = {"cep": cliente.cep, **resultado.como_dict(),
                                        "em": datetime.now(FUSO_BRASILIA).isoformat(timespec="seconds")}
                novas += 1
                if resultado.cep_automatico and APLICAR_CEP_DA_RECEITA_AUTOMATICAMENTE:
                    correcoes[venda.pedido] = {
                        **(corrigida or {}), "cep": resultado.cep_automatico,
                        "de": (corrigida or {}).get("de", cliente.cep), "origem": "receita",
                        "usuario": "automático (cadastro do CNPJ na Receita Federal)",
                        "em": datetime.now(FUSO_BRASILIA).isoformat(timespec="seconds")}
                    corrigidas = True
                    auditar(f"CEP do pedido {venda.pedido} corrigido automaticamente pelo cadastro do CNPJ na Receita "
                            f"Federal: {formatar_cep(cliente.cep)} -> {formatar_cep(resultado.cep_automatico)}")
                else:
                    problemas += resultado.status in ("divergente", "nao_encontrado")
            gravar_json(arquivo, feitas)  # não perde o andamento se a página cair
            if corrigidas:
                gravar_json(pasta / "correcoes_cep.json", correcoes)
    gravar_json(arquivo, feitas)
    if corrigidas:
        gravar_json(pasta / "correcoes_cep.json", correcoes)
    pedidos = {v.pedido for v in vendas}
    indisponiveis = sum(1 for p, v in feitas.items() if p in pedidos and v["status"] == "indisponivel")
    return novas, problemas, restantes, indisponiveis


def completar_sugestoes_de_rua(pasta: Path, vendas, orcamento: float = 60) -> int:
    """Segunda etapa: busca (no ViaCEP) os CEPs por nome de rua das vendas já sinalizadas. Devolve quantas faltam."""
    with trava_verificacao:
        feitas = ler_json(pasta / "cep.json")
        correcoes = ler_json(pasta / "correcoes_cep.json")
        pendentes = [v for v in vendas
                     if feitas.get(v.pedido, {}).get("ruas_pendentes") and feitas[v.pedido]["cep"] == v.cliente.cep
                     and not (v.pedido in correcoes and correcoes[v.pedido]["cep"] != correcoes[v.pedido]["de"])
                     and not getattr(v, "cep_lembrada", False)]
        inicio, feitos = time.monotonic(), 0

        def completar(venda):
            c = venda.cliente
            try:
                return servico_cep.verificar(c.cep, c.cod_municipio, c.uf, c.logradouro, c.bairro, c.documento,
                                             buscar_ruas=True)
            except Exception:
                return None

        with ThreadPoolExecutor(THREADS_VERIFICACAO_CEP) as pool:
            for i in range(0, len(pendentes), THREADS_VERIFICACAO_CEP):
                if time.monotonic() - inicio >= orcamento:
                    break
                bloco = pendentes[i : i + THREADS_VERIFICACAO_CEP]
                for venda, resultado in zip(bloco, pool.map(completar, bloco)):
                    if resultado is not None and resultado.status != "indisponivel":
                        feitas[venda.pedido] = {"cep": venda.cliente.cep, **resultado.como_dict(),
                                                "em": datetime.now(FUSO_BRASILIA).isoformat(timespec="seconds")}
                        feitos += 1
                gravar_json(pasta / "cep.json", feitas)
        return max(0, len(pendentes) - feitos)


@app.post("/lote/<lote_id>/verificar-ceps")
def verificar_ceps(lote_id):
    """Confere o CEP de cada venda com o município do cadastro (ViaCEP/IBGE) e procura o CEP correto."""
    pasta, remessa, ambiente = carregar(lote_id)
    vendas = vendas_a_verificar(pasta, remessa, ambiente)
    novas, problemas, restantes, indisponiveis = verificar_lista(pasta, vendas)
    sem_sugestao = 0 if restantes else completar_sugestoes_de_rua(pasta, vendas, 90)
    auditar(f"verificou CEPs do lote {lote_id}: {novas} consultados, {problemas} com problema")
    flash(f"CEPs verificados: {novas} nesta rodada, {problemas} com problema."
          + (f" Faltam {restantes}: clique de novo para continuar." if restantes else " Verificação concluída.")
          + (f" Ainda procurando CEPs sugeridos para {sem_sugestao} vendas: clique de novo." if sem_sugestao else "")
          + (f" {indisponiveis} não puderam ser consultados (serviço de CEP indisponível): tente de novo." if indisponiveis else ""),
          "erro" if problemas or restantes or indisponiveis else "ok")
    return redirect(url_for("lote", lote_id=lote_id, motivo=MOTIVO_CEP if problemas else "") + "#vendas")


@app.post("/lote/<lote_id>/aplicar-cep")
def aplicar_cep_rota(lote_id):
    """A equipe confirma um dos CEPs sugeridos: vale só para este pedido, neste lote, e fica registrado."""
    pasta, remessa, _ = carregar(lote_id)
    pedido, novo = request.form.get("pedido", ""), re.sub(r"\D", "", request.form.get("cep", ""))
    venda = remessa.venda(pedido)
    if not venda or len(novo) != 8 or novo not in {s["cep"] for s in venda.sugestoes_cep}:
        flash("CEP não está entre as sugestões da verificação.", "erro")
        return redirect(url_for("lote", lote_id=lote_id) + "#vendas")
    if (pasta / "envios.json").exists() and getattr(ControleEnvios(pasta).envios.get(pedido), "situacao", "") == AUTORIZADA:
        flash("Pedido já emitido: o CEP não pode mais ser alterado.", "erro")
        return redirect(url_for("lote", lote_id=lote_id) + "#vendas")
    correcoes = ler_json(pasta / "correcoes_cep.json")
    correcoes[pedido] = {**correcoes.get(pedido, {}), "cep": novo,
                         "de": correcoes.get(pedido, {}).get("de", venda.cliente.cep),
                         "usuario": (usuario_atual() or {}).get("login", "-"),
                         "em": datetime.now(FUSO_BRASILIA).isoformat(timespec="seconds")}
    gravar_json(pasta / "correcoes_cep.json", correcoes)
    lembrar_correcao(venda, correcoes[pedido])
    auditar(f"aplicou o CEP {formatar_cep(novo)} (era {formatar_cep(venda.cliente.cep)}) ao pedido {pedido} no lote {lote_id}")
    flash(f"Pedido {pedido}: CEP {formatar_cep(novo)} aplicado. Atualize também o cadastro no Gerência para as próximas remessas.", "ok")
    return redirect(url_for("lote", lote_id=lote_id) + "#vendas")


@app.post("/lote/<lote_id>/aplicar-municipio")
def aplicar_municipio_rota(lote_id):
    """O CEP e a rua/bairro existem em OUTRO município: a equipe confirma que o município do cadastro está
    errado e a nota sai com o município do CEP. Só vale quando a verificação indicou essa alternativa."""
    pasta, remessa, _ = carregar(lote_id)
    pedido = request.form.get("pedido", "")
    venda = remessa.venda(pedido)
    alternativa = getattr(venda, "alternativa_municipio", None) if venda else None
    if not alternativa:
        flash("A verificação não indicou outro município para esse pedido.", "erro")
        return redirect(url_for("lote", lote_id=lote_id) + "#vendas")
    if (pasta / "envios.json").exists() and getattr(ControleEnvios(pasta).envios.get(pedido), "situacao", "") == AUTORIZADA:
        flash("Pedido já emitido: o município não pode mais ser alterado.", "erro")
        return redirect(url_for("lote", lote_id=lote_id) + "#vendas")
    correcoes = ler_json(pasta / "correcoes_cep.json")
    atual = correcoes.get(pedido, {})
    correcoes[pedido] = {
        **atual, "cep": venda.cliente.cep, "de": atual.get("de", venda.cliente.cep),
        "cod_municipio": alternativa["ibge"], "uf": alternativa["uf"], "cidade": alternativa["cidade"],
        "de_municipio": venda.cliente.cod_municipio, "de_uf": venda.cliente.uf,
        "de_cidade": servico_cep.nome_do_municipio(venda.cliente.cod_municipio) or venda.cliente.cod_municipio,
        "usuario": (usuario_atual() or {}).get("login", "-"),
        "em": datetime.now(FUSO_BRASILIA).isoformat(timespec="seconds"),
    }
    gravar_json(pasta / "correcoes_cep.json", correcoes)
    lembrar_correcao(venda, correcoes[pedido])
    auditar(f"corrigiu o município do pedido {pedido} no lote {lote_id}: {correcoes[pedido]['de_cidade']}/"
            f"{venda.cliente.uf} -> {alternativa['cidade']}/{alternativa['uf']} (CEP {formatar_cep(venda.cliente.cep)})")
    flash(f"Pedido {pedido}: município do tomador corrigido para {alternativa['cidade']}/{alternativa['uf']}. "
          "Corrija também o cadastro do cliente no Gerência.", "ok")
    return redirect(url_for("lote", lote_id=lote_id) + "#vendas")


def gravar_correcao_endereco(pasta: Path, venda, cep: str, municipio: dict | None) -> None:
    """Registra a correção de CEP (e, se mudou, do município) de um pedido; vale só para ele, neste lote."""
    correcoes = ler_json(pasta / "correcoes_cep.json")
    atual = correcoes.get(venda.pedido, {})
    nova = {**atual, "cep": cep, "de": atual.get("de", venda.cliente.cep),
            "usuario": (usuario_atual() or {}).get("login", "-"),
            "em": datetime.now(FUSO_BRASILIA).isoformat(timespec="seconds")}
    if municipio and municipio["ibge"] != venda.cliente.cod_municipio:
        nova.update({
            "cod_municipio": municipio["ibge"], "uf": municipio["uf"], "cidade": municipio["cidade"],
            "de_municipio": atual.get("de_municipio", venda.cliente.cod_municipio),
            "de_uf": atual.get("de_uf", venda.cliente.uf),
            "de_cidade": atual.get("de_cidade") or servico_cep.nome_do_municipio(venda.cliente.cod_municipio)
            or venda.cliente.cod_municipio})
    correcoes[venda.pedido] = nova
    gravar_json(pasta / "correcoes_cep.json", correcoes)
    lembrar_correcao(venda, nova)


def pedido_ja_emitido(pasta: Path, pedido: str) -> bool:
    return (pasta / "envios.json").exists() and getattr(ControleEnvios(pasta).envios.get(pedido), "situacao", "") == AUTORIZADA


@app.post("/lote/<lote_id>/aplicar-endereco-receita")
def aplicar_endereco_receita_rota(lote_id):
    """O endereço do CNPJ na Receita Federal fica em outra cidade que o do Gerência: a equipe confirma e a nota
    sai com o CEP e o município da Receita. Só vale quando a verificação trouxe esse endereço."""
    pasta, remessa, _ = carregar(lote_id)
    pedido = request.form.get("pedido", "")
    venda = remessa.venda(pedido)
    endereco = getattr(venda, "endereco_receita", None) if venda else None
    if not endereco:
        flash("A verificação não trouxe endereço da Receita Federal para esse pedido.", "erro")
    elif pedido_ja_emitido(pasta, pedido):
        flash("Pedido já emitido: o endereço não pode mais ser alterado.", "erro")
    else:
        escolhido = re.sub(r"\D", "", request.form.get("cep", "")) or endereco["cep"]
        if escolhido != endereco["cep"] and escolhido not in {s["cep"] for s in endereco.get("alternativas", [])}:
            flash("CEP não está entre as opções da verificação.", "erro")
            return redirect(url_for("lote", lote_id=lote_id) + "#vendas")
        antes = f"{formatar_cep(venda.cliente.cep)} {venda.cliente.uf}"
        gravar_correcao_endereco(pasta, venda, escolhido, endereco)
        auditar(f"aplicou o endereço do CNPJ na Receita Federal ao pedido {pedido} no lote {lote_id}: {antes} -> "
                f"{formatar_cep(escolhido)} {endereco['cidade']}/{endereco['uf']}")
        flash(f"Pedido {pedido}: CEP {formatar_cep(escolhido)} e município {endereco['cidade']}/{endereco['uf']} "
              "aplicados (endereço do CNPJ na Receita Federal). Corrija também o cadastro no Gerência.", "ok")
    return redirect(url_for("lote", lote_id=lote_id) + "#vendas")


@app.post("/lote/<lote_id>/corrigir-cep")
def corrigir_cep_manual(lote_id):
    """CEP digitado pela equipe (ex.: lido no cartão CNPJ). O CEP precisa existir nos Correios; o município e a
    UF passam a ser os do CEP."""
    pasta, remessa, _ = carregar(lote_id)
    pedido, novo = request.form.get("pedido", ""), re.sub(r"\D", "", request.form.get("cep", ""))
    venda = remessa.venda(pedido)
    if not venda or len(novo) != 8:
        flash("Informe um CEP com 8 dígitos.", "erro")
    elif pedido_ja_emitido(pasta, pedido):
        flash("Pedido já emitido: o CEP não pode mais ser alterado.", "erro")
    else:
        dados = servico_cep.dados_do_cep(novo)
        if dados is None:
            flash("Serviço de CEP indisponível agora. Tente de novo em instantes.", "erro")
        elif not dados or not dados.get("ibge"):
            flash(f"O CEP {formatar_cep(novo)} não existe nos Correios. Confira o número.", "erro")
        else:
            municipio = {"ibge": dados["ibge"], "uf": dados["uf"], "cidade": dados["cidade"]}
            antes = f"{formatar_cep(venda.cliente.cep)} {venda.cliente.uf}"
            gravar_correcao_endereco(pasta, venda, novo, municipio)
            auditar(f"digitou o CEP do pedido {pedido} no lote {lote_id}: {antes} -> {formatar_cep(novo)} "
                    f"{dados['cidade']}/{dados['uf']}")
            flash(f"Pedido {pedido}: CEP {formatar_cep(novo)} aplicado ({dados['cidade']}/{dados['uf']}"
                  f"{', ' + dados['logradouro'] if dados.get('logradouro') else ''}). "
                  "Corrija também o cadastro no Gerência.", "ok")
    return redirect(url_for("lote", lote_id=lote_id) + "#vendas")


@app.get("/lote/<lote_id>/ceps.csv")
def ceps_csv(lote_id):
    """Lista de CEPs a corrigir no cadastro do Gerência (pedido, cliente, CEP atual, sugestões)."""
    pasta, remessa, _ = carregar(lote_id)
    linhas = ["pedido;cliente;cnpj_cpf;uf;cod_municipio;cep_atual;situacao;cidade_do_cep_atual;cep_sugerido;rua_sugerida;bairro_sugerido;"
              "municipio_provavel"]
    verificacoes = ler_json(pasta / "cep.json")
    for venda in remessa.vendas:
        achado = verificacoes.get(venda.pedido)
        if not achado or achado["cep"] != venda.cliente.cep or achado["status"] in ("ok", "indisponivel"):
            continue
        base = [venda.pedido, venda.cliente.razao_social, venda.cliente.documento, venda.cliente.uf,
                venda.cliente.cod_municipio, formatar_cep(venda.cliente.cep), achado["status"], achado["cidade_do_cep"]]
        alt = achado.get("alternativa_municipio") or {}
        municipio = f"{alt['cidade']}/{alt['uf']} (IBGE {alt['ibge']})" if alt else ""
        for sug in achado["sugestoes"] or [{"cep": "", "logradouro": "", "bairro": ""}]:
            linhas.append(";".join(csv_seguro(c) for c in base + [formatar_cep(sug["cep"]) if sug["cep"] else "",
                                                                  sug["logradouro"], sug["bairro"], municipio]))
    conteudo = ("﻿" + "\r\n".join(linhas) + "\r\n").encode("utf-8")  # BOM: o Excel abre com acentos
    return send_file(io.BytesIO(conteudo), mimetype="text/csv", as_attachment=True, download_name=f"ceps_a_corrigir_{lote_id}.csv")


@app.get("/auditoria")
@somente_admin
def auditoria():
    return render_template("auditoria.html", linhas=ultimas_auditorias(500))


@app.post("/importar")
def importar():
    arquivo = request.files.get("remessa")
    if not arquivo or not arquivo.filename:
        flash("Selecione o arquivo de remessa.", "erro")
        return redirect(url_for("inicio"))
    conteudo = arquivo.read()
    remessa = ler_remessa(conteudo, arquivo.filename)
    if not remessa.vendas:
        flash("Nenhuma venda encontrada. Confira se o arquivo é a remessa do Gerência.", "erro")
        return redirect(url_for("inicio"))
    lote_id, pasta = criar_pasta_lote()
    (pasta / "remessa.txt").write_bytes(conteudo)
    (pasta / "nome_original.txt").write_text(Path(arquivo.filename).name, "utf-8")
    auditar(f"importou a remessa {Path(arquivo.filename).name} ({len(remessa.vendas)} vendas) como lote {lote_id}")
    if getattr(config, "VERIFICAR_CEPS_AO_IMPORTAR", True):
        verificar_ceps_em_segundo_plano(lote_id)
    return redirect(url_for("lote", lote_id=lote_id))


@app.get("/lote/<lote_id>")
def lote(lote_id):
    historico_cep().atualizar(5)  # respostas do ISS Net desde a última vez: a lista de CEPs recusados fica em dia
    pasta, remessa, ambiente = carregar(lote_id)
    serie = serie_do_lote(pasta)
    registro = registro_de(ambiente, serie)
    ocultas, filhos, retorno_remessa = 0, [], None
    if ambiente != "producao":
        # Vendas que já estão em algum lote de produção somem da remessa: não há como mandá-las de novo.
        em_producao = pedidos_ja_em_producao()
        visiveis = [v for v in remessa.vendas if v.pedido not in em_producao]
        ocultas = len(remessa.vendas) - len(visiveis)
        remessa = Remessa(remessa.nome_arquivo, visiveis, remessa.erros_arquivo, remessa.sem_nota)
        filhos = [resumo_lote(p) for p in filhos_de(lote_id)]
        aptas_filhos = [(p, n) for p in filhos_de(lote_id) for n in notas_aptas(p)]
        retorno_remessa = {
            "todas": len(aptas_filhos),
            "novas": sum(1 for p, (_, nota) in aptas_filhos if nota.numero not in ja_retornadas(p)),
        }
    filtros = {
        "situacao": request.args.get("situacao", "todas"),
        "motivo": request.args.get("motivo", ""),
        "q": request.args.get("q", ""),
    }
    filtradas = filtrar_vendas(remessa.vendas, filtros["situacao"], filtros["motivo"], filtros["q"])
    try:
        pagina = max(1, int(request.args.get("pagina", 1)))
    except ValueError:
        pagina = 1
    total_paginas = max(1, -(-len(filtradas) // POR_PAGINA))
    pagina = min(pagina, total_paginas)
    inicio_pagina = (pagina - 1) * POR_PAGINA

    def link_pagina(numero):
        return url_for("lote", lote_id=lote_id, pagina=numero, **filtros) + "#vendas"

    paginas = [(n, link_pagina(n) if n else None) for n in numeros_de_pagina(pagina, total_paginas)]
    feitas_cep = ler_json(pasta / "cep.json")
    pedidos_do_lote ={v.pedido for v in remessa.vendas}
    if ambiente != "producao" and getattr(config, "VERIFICAR_CEPS_AO_IMPORTAR", True) and not verificando_ceps(lote_id):
        corrigidos = ler_json(pasta / "correcoes_cep.json")
        if any(v.pedido not in feitas_cep and v.pedido not in corrigidos and not v.cep_lembrada for v in remessa.vendas):
            verificar_ceps_em_segundo_plano(lote_id)  # remessa que ficou com CEPs por conferir: continua sozinho
    notas, erros_notas = notas_do_lote(pasta)
    resultados = associar(remessa, notas, registro, serie) if notas else []
    envios = ControleEnvios(pasta).envios
    arquivo_links = pasta / "links.json"
    documentos = json.loads(arquivo_links.read_text("utf-8")) if arquivo_links.exists() else {}
    return render_template(
        "lote.html",
        lote_id=lote_id,
        remessa=remessa,
        ambiente=ambiente,
        serie=serie,
        documentos=documentos,
        perfil_envio=perfil_confirmado(),
        envios=sorted(envios.values(), key=lambda e: e.numero_dps),
        situacao_envios=Counter(e.situacao for e in envios.values()),
        a_enviar=sum(1 for v in remessa.prontas if getattr(envios.get(v.pedido), "situacao", "") != AUTORIZADA),
        vendas=filtradas[inicio_pagina : inicio_pagina + POR_PAGINA],
        total_filtradas=len(filtradas),
        ceps_recusados=sum(1 for v in remessa.vendas if v.cep_recusado),
        retencao_pendente={v.pedido for v in remessa.vendas if getattr(v, "retencao_pendente", False)},
        sem_nota_pendente={v.pedido for v in remessa.vendas if getattr(v, "sem_nota_pendente", False)},
        motivo_cep_recusado=MOTIVO_CEP_RECUSADO,
        inicio_pagina=inicio_pagina,
        pagina=pagina,
        total_paginas=total_paginas,
        paginas=paginas,
        link_anterior=link_pagina(pagina - 1) if pagina > 1 else None,
        link_proxima=link_pagina(pagina + 1) if pagina < total_paginas else None,
        filtros=filtros,
        motivos=Counter(motivo(m) for v in remessa.vendas for m in v.erros + v.avisos).most_common(),
        situacao=situacao,
        por_pacote=Counter(v.pacote or "não identificado" for v in remessa.vendas).most_common(),
        ja_numerados=registro.ja_numerados([v.pedido for v in remessa.prontas]),
        dps_geradas=len(list((pasta / "dps").glob("*.xml"))),
        resultados=resultados,
        aptas=sum(r.apta for r in resultados),
        aptas_novas=sum(1 for r in resultados if r.apta and r.nota.numero not in ja_retornadas(pasta)),
        retornos=historico_retornos(pasta)[::-1],
        erros_notas=erros_notas,
        serie_pronta=(pasta / "serie.txt").exists() and not vendas_sem_numero_reservado(pasta, remessa, ambiente, serie),
        ocultas=ocultas,
        filhos=filhos,
        retorno_remessa=retorno_remessa,
        lote_origem=origem_do_lote(pasta),
        verificando_ceps=verificando_ceps(lote_id),
        ceps_verificados=len(pedidos_do_lote & feitas_cep.keys()),
    )


@app.post("/lote/<lote_id>/gerar-dps")
def gerar_dps(lote_id):
    pasta, remessa, ambiente = carregar(lote_id)
    destino = pasta / "dps"
    destino.mkdir(exist_ok=True)
    numeros = registro_de(ambiente, serie_do_lote(pasta)).numeros_para([v.pedido for v in remessa.prontas])
    agora = datetime.now(FUSO_BRASILIA)
    tp_amb = 1 if ambiente == "producao" else 2
    for venda in remessa.prontas:
        numero = numeros[venda.pedido]
        nome = f"DPS_{config.SERIE_DPS}_{numero:015d}_pedido_{venda.pedido}.xml"
        (destino / nome).write_bytes(montar_dps(venda, config.SERIE_DPS, numero, agora, tp_amb,
                                                getattr(venda, "iss_retido", False)))
    flash(f"{len(remessa.prontas)} DPS geradas. Simulação: nada foi assinado nem enviado ao ISS Net.", "ok")
    return redirect(url_for("lote", lote_id=lote_id))


@app.route("/certificado", methods=["GET", "POST"])
def certificado_a1():
    if request.method == "POST":
        arquivo = request.files.get("pfx")
        if not arquivo or not arquivo.filename:
            flash("Selecione o arquivo .pfx do certificado.", "erro")
            return redirect(url_for("certificado_a1"))
        try:
            certificado = CertificadoA1(arquivo.read(), request.form.get("senha", ""))
        except ValueError as erro:
            flash(str(erro), "erro")
            return redirect(url_for("certificado_a1"))
        if certificado.cnpj[:8] != config.PRESTADOR["cnpj"][:8]:
            flash(f"Este certificado é do CNPJ {certificado.cnpj or '?'}, não da empresa emissora.", "erro")
            return redirect(url_for("certificado_a1"))
        if certificado.dias_para_vencer < 0:
            flash("Certificado vencido.", "erro")
            return redirect(url_for("certificado_a1"))
        sessao_certificado["certificado"] = certificado
        sessao_certificado["clientes"] = {}
        auditar(f"carregou o certificado {certificado.titular} (válido até {certificado.valido_ate:%d/%m/%Y})")
        flash(f"Certificado carregado: {certificado.titular}.", "ok")
        destino = request.form.get("voltar", "")
        return redirect(destino if destino.startswith("/lote/") else url_for("inicio"))
    return render_template("certificado.html", voltar=request.args.get("voltar", ""))


@app.post("/certificado/consultar-cadastro")
def consultar_cadastro():
    ambiente = request.form.get("ambiente", "homologacao")
    if ambiente not in AMBIENTES:
        abort(400)
    voltar = request.form.get("voltar", "")
    destino = redirect(voltar + "#emissao" if voltar.startswith("/lote/") else url_for("certificado_a1"))
    cliente = obter_cliente(ambiente)
    if cliente is None:
        return destino
    try:
        resposta, resumo = cliente.consultar_dados_cadastrais()
    except ErroComunicacao as erro:
        flash(f"Não foi possível consultar o cadastro ({ambiente}): {erro}", "erro")
        return destino
    if resumo:
        partes = [f"{campo}: {', '.join(v) if isinstance(v, list) else v}" for campo, v in resumo.items()]
        flash(f"Cadastro encontrado no ISS Net ({ambiente}, {cliente.url}) — " + " | ".join(partes), "ok")
    else:
        flash(f"ISS Net ({ambiente}) não retornou cadastro: "
              + (" | ".join(str(m) for m in resposta.erros) or resposta.xml.decode("utf-8", "replace")[:300]), "erro")
    if cliente.aviso_wsdl:
        flash(cliente.aviso_wsdl, "erro")
    return destino


@app.post("/certificado/consultar-dps-disponivel")
def consultar_dps_disponivel():
    ambiente = request.form.get("ambiente", "homologacao")
    if ambiente not in AMBIENTES:
        abort(400)
    voltar = request.form.get("voltar", "")
    destino = redirect(voltar + "#emissao" if voltar.startswith("/lote/") else url_for("certificado_a1"))
    cliente = obter_cliente(ambiente)
    if cliente is None:
        return destino
    try:
        resposta, disponiveis = cliente.consultar_dps_disponivel()
    except ErroComunicacao as erro:
        flash(f"Não foi possível consultar as DPS disponíveis ({ambiente}): {erro}", "erro")
        return destino
    if disponiveis:
        series = Counter(serie for serie, _ in disponiveis)
        flash(f"DPS disponíveis no ISS Net ({ambiente}): {len(disponiveis)} na página 1 — séries "
              + ", ".join(f"{s} ({q})" for s, q in series.items())
              + " — exemplos: " + ", ".join(f"{s}/{n}" for s, n in disponiveis[:10]), "ok")
    else:
        flash(f"ISS Net ({ambiente}) não listou DPS disponíveis: "
              + (" | ".join(str(m) for m in resposta.erros) or resposta.xml.decode("utf-8", "replace")[:300]), "erro")
    return destino


@app.post("/certificado/remover")
def remover_certificado():
    sessao_certificado.update(certificado=None, clientes={})
    auditar("removeu o certificado da memória")
    flash("Certificado removido da memória do sistema.", "ok")
    return redirect(url_for("certificado_a1"))


@app.post("/lote/<lote_id>/validar-issnet")
def validar_no_issnet(lote_id):
    pasta, remessa, ambiente = carregar(lote_id)
    cliente = obter_cliente(ambiente)
    if cliente is None or not remessa.prontas:
        return voltar_emissao(lote_id)
    venda = remessa.prontas[0]
    # Validação não consome número de DPS: usa um número fora da faixa real.
    xml = dps_assinada(venda, 999999999, cliente.certificado, datetime.now(FUSO_BRASILIA), tp_amb=cliente.tp_amb)
    dados = envelope_gerar_nfse(xml).encode("utf-8")
    if cliente.formato is None:
        relatorio = cliente.descobrir_formato(dados)
        if cliente.formato is None:
            flash("O ISS Net não aceitou nenhum formato de envio: " + " || ".join(relatorio), "erro")
        else:
            flash(f"Formato de envio confirmado pelo ISS Net: {cliente.formato}. Testes: " + " || ".join(relatorio), "ok")
        return voltar_emissao(lote_id)
    try:
        resposta = cliente.validar_xml(dados)
    except ErroComunicacao as erro:
        flash(f"Não foi possível validar no ISS Net ({ambiente}): {erro}", "erro")
        return voltar_emissao(lote_id)
    if resposta.erros:
        flash(f"ISS Net ({ambiente}) apontou: " + " | ".join(str(m) for m in resposta.erros), "erro")
    else:
        detalhe = " | ".join(str(m) for m in resposta.alertas) or resposta.xml.decode("utf-8", "replace")[:300]
        flash(f"ISS Net ({ambiente}) validou a DPS do pedido {venda.pedido} sem erros (nada foi emitido). {detalhe}", "ok")
    return voltar_emissao(lote_id)


def vendas_sem_numero_reservado(pasta: Path, remessa, ambiente: str, serie: str) -> int:
    """Quantas vendas ainda não emitidas do lote não têm número de DPS reservado na série do lote."""
    envios = ControleEnvios(pasta).envios
    pedidos = [v.pedido for v in remessa.prontas if getattr(envios.get(v.pedido), "situacao", "") != AUTORIZADA]
    com_numero = set(registro_de(ambiente, serie).ja_numerados(pedidos))
    return sum(1 for p in pedidos if p not in com_numero)


def pronto_para_emitir(remessa, ambiente: str) -> ClienteIssNet | None:
    """Confere certificado, formato, perfil e — em produção — endereço, tamanho do lote e confirmação."""
    cliente = obter_cliente(ambiente)
    if cliente is None:
        return None
    if cliente.formato is None:
        flash("Antes de emitir, clique em Validar: o sistema precisa confirmar com o ISS Net o formato de envio.", "erro")
        return None
    if cliente.perfil is None:
        flash("Antes de emitir o lote, rode o Diagnóstico de assinatura (envia uma única venda).", "erro")
        return None
    if ambiente == "producao":
        if not cliente.wsdl_ok:
            flash(f"Endereço de produção do ISS Net não confirmado — emissão bloqueada. {cliente.aviso_wsdl}", "erro")
            return None
        if len(remessa.prontas) > config.LIMITE_NOTAS_PRODUCAO_POR_LOTE:
            flash(f"Lote de produção com mais de {config.LIMITE_NOTAS_PRODUCAO_POR_LOTE} vendas — bloqueado.", "erro")
            return None
        if request.form.get("confirmo") != "sim":
            flash("Marque a confirmação de emissão de notas reais antes de emitir.", "erro")
            return None
    return cliente


def pedidos_ja_em_producao() -> set[str]:
    """Pedidos que já estão em algum lote de produção ou têm número de DPS de produção."""
    pedidos = set()
    for arquivo in config.DADOS_DIR.glob("numeracao_producao_serie_*.json"):
        pedidos |= set(json.loads(arquivo.read_text("utf-8")).get("pedidos", {}))
    if LOTES_DIR.exists():
        for pasta in LOTES_DIR.iterdir():
            if pasta.is_dir() and RE_LOTE.match(pasta.name) and ambiente_do_lote(pasta) == "producao":
                pedidos |= {v.pedido for v in ler_remessa((pasta / "remessa.txt").read_bytes()).vendas}
    return pedidos


def escolher_para_producao(remessa, quantidade: int, excluir: set[str]):
    """Vendas OK (sem aviso), de clientes com uma venda só, fora de produção, alternando os pacotes
    (mensal, anual, avulso, semestral) para o teste cobrir as composições de valor diferentes."""
    por_cliente = Counter(v.cliente.documento for v in remessa.vendas)
    candidatas = [v for v in remessa.vendas
                  if v.pronta and not v.avisos and por_cliente[v.cliente.documento] == 1 and v.pedido not in excluir]
    filas = {p: [v for v in candidatas if v.pacote == p] for p in ("mensal", "anual", "avulso", "semestral")}
    filas["outros"] = [v for v in candidatas if v.pacote not in filas]
    escolhidas = []
    while len(escolhidas) < quantidade and any(filas.values()):
        for fila in filas.values():
            if fila and len(escolhidas) < quantidade:
                escolhidas.append(fila.pop(0))
    return escolhidas


@app.post("/lote/<lote_id>/criar-lote-producao")
def criar_lote_producao(lote_id):
    """Cria um lote de PRODUÇÃO a partir de um lote importado. Não emite nada.

    modo "todas": todas as vendas prontas (OK e Atenção) que ainda não estão em produção.
    modo "proximas": as próximas N vendas prontas da remessa (ex.: de 10 em 10).
    modo "amostra": até LIMITE_AMOSTRA_PRODUCAO vendas OK, uma de cada pacote (teste).
    Uma venda que entra num lote de produção some da remessa e não pode entrar em outro lote."""
    with trava_criacao:  # dois usuários criando lotes ao mesmo tempo não pegam as mesmas vendas
        return _criar_lote_producao(lote_id)


trava_criacao = threading.Lock()


def escolher_verificando_ceps(lote_id: str, pasta: Path, candidatas, quantidade: int | None):
    """Pega as próximas vendas (N, ou todas) já com o CEP verificado: vendas com CEP/município incompatível
    ficam de fora e outras entram no lugar, até completar N. Devolve (escolhidas, bloqueadas, indisponiveis,
    mensagem_de_erro). Se o tempo da verificação acabar, devolve a mensagem e nada é criado: repetir continua
    de onde parou (o resultado fica em cep.json)."""
    escolhidas, bloqueadas, indisponiveis = [], [], 0
    inicio, i = time.monotonic(), 0
    while i < len(candidatas) and (quantidade is None or len(escolhidas) < quantidade):
        faltam = len(candidatas) - i if quantidade is None else quantidade - len(escolhidas)
        bloco = candidatas[i : i + faltam]
        i += len(bloco)
        tempo = max(0.0, ORCAMENTO_VERIFICACAO_CEP - (time.monotonic() - inicio))
        _, _, sem_tempo, indisp = verificar_lista(pasta, bloco, tempo)
        indisponiveis += indisp
        if sem_tempo:
            return None, None, 0, (f"Verificando os CEPs antes de criar o lote: ainda faltam {sem_tempo} vendas. "
                                   "Clique em Criar lote de novo para continuar de onde parou.")
        _, atual, _ = carregar(lote_id)
        for venda in bloco:
            nova = atual.venda(venda.pedido)
            (escolhidas if nova.pronta else bloqueadas).append(nova)
    return escolhidas, bloqueadas, indisponiveis, ""


def _criar_lote_producao(lote_id):
    pasta, remessa, ambiente = carregar(lote_id)
    if ambiente != "homologacao":
        abort(400)
    excluir = pedidos_ja_em_producao()
    candidatas = [v for v in remessa.prontas if v.pedido not in excluir]
    modo = request.form.get("modo")
    bloqueadas, indisponiveis = [], 0
    if modo in ("todas", "proximas"):
        quantidade = None
        if modo == "proximas":
            try:
                quantidade = int(request.form.get("quantidade", "0"))
            except ValueError:
                quantidade = 0
            if quantidade < 1:
                flash("Informe quantas vendas o lote deve ter.", "erro")
                return redirect(url_for("lote", lote_id=lote_id) + "#emissao")
        # O CEP de cada venda é conferido ANTES de entrar no lote: assim o lote só leva vendas que o ISS Net aceita.
        escolhidas, bloqueadas, indisponiveis, erro = escolher_verificando_ceps(lote_id, pasta, candidatas, quantidade)
        if erro:
            flash(erro, "erro")
            return redirect(url_for("lote", lote_id=lote_id) + "#emissao")
        if len(escolhidas) > config.LIMITE_NOTAS_PRODUCAO_POR_LOTE:
            flash(f"{len(escolhidas)} vendas passam do limite de {config.LIMITE_NOTAS_PRODUCAO_POR_LOTE} por lote.", "erro")
            return redirect(url_for("lote", lote_id=lote_id))
    else:
        try:
            quantidade = int(request.form.get("quantidade", "0"))
        except ValueError:
            quantidade = 0
        if not 1 <= quantidade <= config.LIMITE_AMOSTRA_PRODUCAO:
            flash(f"Escolha de 1 a {config.LIMITE_AMOSTRA_PRODUCAO} vendas para o lote de teste.", "erro")
            return redirect(url_for("lote", lote_id=lote_id))
        escolhidas = escolher_para_producao(remessa, quantidade, excluir)
    if not escolhidas:
        flash("Não há vendas prontas fora de produção neste lote (todas já estão em algum lote de produção"
              + (", ou as restantes estão com CEP/município incompatível: corrija-as e tente de novo)." if bloqueadas else ")."),
              "erro")
        return redirect(url_for("lote", lote_id=lote_id))
    novo_id, nova = criar_pasta_lote()
    (nova / "remessa.txt").write_bytes(extrair_vendas((pasta / "remessa.txt").read_bytes(), [v.pedido for v in escolhidas]))
    nome = (pasta / "nome_original.txt").read_text("utf-8")
    (nova / "nome_original.txt").write_text(f"PRODUCAO_{len(escolhidas)}notas_{nome}", "utf-8")
    (nova / "ambiente.txt").write_text("producao", "utf-8")
    (nova / "origem.txt").write_text(f"lote {lote_id}", "utf-8")
    pedidos_lote = {v.pedido for v in escolhidas}
    for arquivo_json in ("correcoes_cep.json", "cep.json"):  # leva CEPs corrigidos e verificados da remessa
        dados = ler_json(pasta / arquivo_json)
        if dados:
            gravar_json(nova / arquivo_json, {p: d for p, d in dados.items() if p in pedidos_lote})
    lista = ", ".join(f"{v.pedido} ({v.pacote}, {v.cliente.razao_social[:30]})" for v in escolhidas[:5])
    ja_em_producao = sum(1 for v in remessa.prontas if v.pedido in excluir)
    auditar(f"criou o lote de produção {novo_id} com {len(escolhidas)} vendas da remessa {lote_id} (modo {modo})")
    aviso_cep = ""
    if bloqueadas:
        aviso_cep += (f" {len(bloqueadas)} venda(s) ficaram de fora por CEP/município incompatível "
                      f"({', '.join(v.pedido for v in bloqueadas[:8])}{' …' if len(bloqueadas) > 8 else ''}): "
                      "corrija na remessa, pelas sugestões, e crie outro lote.")
    if indisponiveis:
        aviso_cep += f" {indisponiveis} venda(s) não tiveram o CEP verificado (serviço de CEP indisponível)."
    auditar(f"criou o lote de produção {novo_id} com {len(escolhidas)} vendas da remessa {lote_id} (modo {modo})"
            + (f"; {len(bloqueadas)} bloqueadas por CEP" if bloqueadas else ""))
    flash(f"Lote de PRODUÇÃO criado com {len(escolhidas)} vendas: {lista}"
          + (" e outras" if len(escolhidas) > 5 else "")
          + f". Elas saíram da lista da remessa. Já estavam em produção antes: {ja_em_producao}. Nada foi emitido ainda."
          + aviso_cep, "erro" if bloqueadas or indisponiveis else "ok")
    return redirect(url_for("lote", lote_id=novo_id))


@app.post("/lote/<lote_id>/usar-serie-autorizada")
def usar_serie_autorizada(lote_id):
    """Consulta no ISS Net os números de DPS disponíveis (só leitura), reserva um para cada pedido do
    lote que ainda não tem nota e grava a série no lote. Não emite nada."""
    pasta, remessa, ambiente = carregar(lote_id)
    cliente = obter_cliente(ambiente)
    if cliente is None:
        return voltar_emissao(lote_id)
    controle = ControleEnvios(pasta)
    pedidos = [v.pedido for v in remessa.prontas
               if getattr(controle.envios.get(v.pedido), "situacao", "") != AUTORIZADA]
    disponiveis: list[tuple[str, str]] = []
    serie, numeros = "", []
    try:
        # Lê páginas (50 números cada) até ter números livres suficientes para os pedidos do lote.
        for pagina in range(1, PAGINAS_DPS_DISPONIVEIS + 1):
            _, achados = cliente.consultar_dps_disponivel(pagina)
            disponiveis += achados
            por_serie = Counter(s for s, _ in disponiveis if s not in SERIES_DE_OUTROS_EMISSORES)
            if por_serie:
                serie = por_serie.most_common(1)[0][0]
                ja_tem = set(registro_de(ambiente, serie).ja_numerados(pedidos))
                usados = registro_de(ambiente, serie).numeros_usados()
                numeros = [int(n) for s, n in disponiveis if s == serie and n.isdigit() and int(n) not in usados]
                if len(numeros) >= len([p for p in pedidos if p not in ja_tem]):
                    break
            if len(achados) < 50:
                break
    except ErroComunicacao as erro:
        flash(f"Não foi possível consultar as DPS disponíveis ({ambiente}): {erro}", "erro")
        return voltar_emissao(lote_id)
    if not serie:
        flash(f"O ISS Net ({ambiente}) não informou números de DPS disponíveis para a empresa.", "erro")
        return voltar_emissao(lote_id)
    registro = registro_de(ambiente, serie)
    novas = registro.reservar(numeros, pedidos)
    com_numero = set(registro.ja_numerados(pedidos))
    sem_numero = [p for p in pedidos if p not in com_numero]
    (pasta / "serie.txt").write_text(serie, "utf-8")
    with open(pasta / "teste_series.log", "a", encoding="utf-8") as log:
        log.write(f"{datetime.now(FUSO_BRASILIA):%d/%m/%Y %H:%M:%S} série autorizada {serie}; "
                  f"reservas: {novas}; sem número: {sem_numero}\n")
    auditar(f"reservou {len(novas)} números da série {serie} no lote {lote_id} (sem número: {len(sem_numero)})")
    exemplos = ", ".join(f"pedido {p} → {n}" for p, n in list(novas.items())[:5])
    flash(f"Série {serie} gravada no lote. {len(novas)} números reservados"
          + (f" ({exemplos}{' …' if len(novas) > 5 else ''})" if novas else " (os pedidos já tinham número)")
          + (f". ATENÇÃO: {len(sem_numero)} pedidos sem número disponível (não serão emitidos): "
             f"{', '.join(sem_numero[:10])}." if sem_numero else ". Todos os pedidos do lote têm número."),
          "erro" if sem_numero else "ok")
    return voltar_emissao(lote_id)


@app.post("/lote/<lote_id>/testar-series")
def testar_series_rota(lote_id):
    """Envia UMA venda tentando as séries informadas, até o ISS Net aceitar a série. Grava a aceita em
    serie.txt do lote (vale para as próximas emissões, sem reiniciar) e o relatório em teste_series.log."""
    pasta, remessa, ambiente = carregar(lote_id)
    cliente = pronto_para_emitir(remessa, ambiente)
    if cliente is None:
        return voltar_emissao(lote_id)
    candidatas = [s.strip().lstrip("0") or "0" for s in request.form.get("candidatas", "").split(",") if s.strip()]
    candidatas = [s for s in candidatas if RE_SERIE.match(s) and s != "0" and s not in SERIES_DE_OUTROS_EMISSORES]
    if not candidatas:
        flash("Informe as séries candidatas (números de 1 a 5 dígitos, separados por vírgula).", "erro")
        return voltar_emissao(lote_id)
    if not trava_emissao.acquire(blocking=False):
        flash("Já existe uma emissão em andamento. Aguarde terminar.", "erro")
        return voltar_emissao(lote_id)
    try:
        controle = ControleEnvios(pasta)
        venda = next((v for v in remessa.prontas
                      if getattr(controle.envios.get(v.pedido), "situacao", "") != AUTORIZADA), None)
        if venda is None:
            flash("Todas as vendas já têm nota.", "ok")
            return voltar_emissao(lote_id)
        aceita, relatorio = testar_series(
            venda, cliente, lambda s: registro_de(ambiente, s), controle, pasta, candidatas
        )
    finally:
        trava_emissao.release()
    with open(pasta / "teste_series.log", "a", encoding="utf-8") as log:
        log.write(f"{datetime.now(FUSO_BRASILIA):%d/%m/%Y %H:%M:%S} pedido {venda.pedido}: "
                  + " || ".join(relatorio) + f" => aceita: {aceita}\n")
    if aceita:
        (pasta / "serie.txt").write_text(aceita, "utf-8")
        flash(f"Série {aceita} aceita pelo ISS Net ({ambiente}). " + " || ".join(relatorio), "ok")
    else:
        flash("Nenhuma das séries foi aceita. " + " || ".join(relatorio), "erro")
    return voltar_emissao(lote_id)


@app.post("/lote/<lote_id>/emitir")
def emitir(lote_id):
    pasta, remessa, ambiente = carregar(lote_id)
    serie = serie_do_lote(pasta)
    if ambiente == "producao":
        faltam_numero = vendas_sem_numero_reservado(pasta, remessa, ambiente, serie)
        if not (pasta / "serie.txt").exists() or faltam_numero:
            flash("Antes de emitir, clique em \"Usar série autorizada com os números disponíveis do ISS Net\": ele "
                  "reserva os números de DPS válidos" + (f" ({faltam_numero} venda(s) ainda sem número)" if faltam_numero else "")
                  + ". Sem isso o ISS Net recusa as notas (série inválida).", "erro")
            return voltar_emissao(lote_id)
    cliente = pronto_para_emitir(remessa, ambiente)
    if cliente is None:
        return voltar_emissao(lote_id)
    limite = config.ENVIO_POR_CLIQUE_PRODUCAO if ambiente == "producao" else LIMITE_ENVIO_POR_CLIQUE
    if not trava_emissao.acquire(blocking=False):
        flash("Já existe uma emissão em andamento. Aguarde terminar.", "erro")
        return voltar_emissao(lote_id)
    try:
        controle = ControleEnvios(pasta)
        pendentes = [v for v in remessa.prontas if getattr(controle.envios.get(v.pedido), "situacao", "") != AUTORIZADA]
        resultado = Counter(
            emitir_venda(v, cliente, registro_de(ambiente, serie), controle, pasta, serie=serie,
                         iss_retido=getattr(v, "iss_retido", False)).situacao
            for v in pendentes[:limite]
        )
    finally:
        trava_emissao.release()
    resumo = ", ".join(f"{qtd} {s}" for s, qtd in resultado.items()) or "nada a enviar"
    auditar(f"emitiu no lote {lote_id} ({ambiente}, série {serie}): {resumo}")
    restantes = max(0, len(pendentes) - limite)
    flash(f"Envio em {ambiente} concluído: {resumo}." + (f" Faltam {restantes}: clique de novo." if restantes else ""),
          "ok" if resultado.get(AUTORIZADA) and not resultado.get(PENDENTE) else "erro")
    return voltar_emissao(lote_id)


@app.post("/lote/<lote_id>/diagnostico-assinatura")
def diagnostico_assinatura(lote_id):
    pasta, remessa, ambiente = carregar(lote_id)
    if ambiente == "producao":
        flash("Diagnóstico de assinatura só em homologação.", "erro")
        return voltar_emissao(lote_id)
    cliente = obter_cliente(ambiente)
    if cliente is None:
        return voltar_emissao(lote_id)
    controle = ControleEnvios(pasta)
    venda = next((v for v in remessa.prontas if getattr(controle.envios.get(v.pedido), "situacao", "") != AUTORIZADA), None)
    if venda is None:
        flash("Todas as vendas já têm nota.", "ok")
        return voltar_emissao(lote_id)
    if not trava_emissao.acquire(blocking=False):
        flash("Já existe uma emissão em andamento. Aguarde terminar.", "erro")
        return voltar_emissao(lote_id)
    try:
        relatorio = diagnosticar_assinatura(venda, cliente, registro_de(ambiente, config.SERIE_DPS), controle, pasta)
    finally:
        trava_emissao.release()
    if cliente.perfil:
        flash(f"Pedido {venda.pedido}: ISS Net aceitou a assinatura no perfil {cliente.perfil.nome}. " + " || ".join(relatorio), "ok")
    else:
        flash(f"Pedido {venda.pedido}: nenhum perfil aceito. " + " || ".join(relatorio), "erro")
    return voltar_emissao(lote_id)


@app.get("/lote/<lote_id>/dps.zip")
def baixar_dps(lote_id):
    pasta = pasta_lote(lote_id)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zip_dps:
        for arquivo in sorted((pasta / "dps").glob("*.xml")):
            zip_dps.write(arquivo, arquivo.name)
    buffer.seek(0)
    return send_file(buffer, mimetype="application/zip", as_attachment=True, download_name=f"dps_{lote_id}.zip")


@app.post("/lote/<lote_id>/buscar-documentos")
def buscar_documentos_rota(lote_id):
    """Só leitura no ISS Net: links oficiais e PDF (DANFSe) das notas autorizadas do lote."""
    pasta, _, ambiente = carregar(lote_id)
    cliente = obter_cliente(ambiente)
    if cliente is None:
        return redirect(url_for("lote", lote_id=lote_id) + "#conferencia")
    relatorio = buscar_documentos(cliente, ControleEnvios(pasta), pasta)
    auditar(f"buscou links/PDFs das notas do lote {lote_id}")
    with open(pasta / "documentos.log", "a", encoding="utf-8") as log:
        log.write(f"{datetime.now(FUSO_BRASILIA):%d/%m/%Y %H:%M:%S} " + " || ".join(relatorio) + "\n")
    flash(" || ".join(relatorio) or "Nenhuma nota autorizada neste lote.",
          "ok" if relatorio and all("PDF salvo" in r for r in relatorio) else "erro")
    return redirect(url_for("lote", lote_id=lote_id) + "#conferencia")


@app.get("/lote/<lote_id>/pdf/<nome>")
def baixar_pdf(lote_id, nome):
    pasta = pasta_lote(lote_id)
    if not re.fullmatch(r"NFSe_\d+_pedido_\w+\.pdf", nome) or not (pasta / "pdfs" / nome).is_file():
        abort(404)
    return send_file(pasta / "pdfs" / nome, mimetype="application/pdf", download_name=nome)


@app.get("/lote/<lote_id>/pdfs.zip")
def baixar_pdfs(lote_id):
    pasta = pasta_lote(lote_id)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zip_pdfs:
        for arquivo in sorted((pasta / "pdfs").glob("*.pdf")):
            zip_pdfs.write(arquivo, arquivo.name)
    buffer.seek(0)
    return send_file(buffer, mimetype="application/zip", as_attachment=True, download_name=f"pdfs_{lote_id}.zip")


@app.get("/lote/<lote_id>/notas.zip")
def baixar_notas(lote_id):
    pasta = pasta_lote(lote_id)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zip_notas:
        for arquivo in sorted((pasta / "notas").glob("*.xml")):
            zip_notas.write(arquivo, arquivo.name)
    buffer.seek(0)
    return send_file(buffer, mimetype="application/zip", as_attachment=True, download_name=f"notas_{lote_id}.zip")


@app.post("/lote/<lote_id>/notas")
def enviar_notas(lote_id):
    pasta = pasta_lote(lote_id)
    destino = pasta / "notas"
    destino.mkdir(exist_ok=True)
    arquivos = [a for a in request.files.getlist("notas") if a.filename]
    for arquivo in arquivos:
        nome = re.sub(r"[^\w.-]", "_", Path(arquivo.filename).name).lstrip(".") or "nota.xml"
        (destino / nome).write_bytes(arquivo.read())
    flash(f"{len(arquivos)} arquivo(s) de nota recebido(s) para conferência.", "ok")
    return redirect(url_for("lote", lote_id=lote_id) + "#conferencia")


def notas_aptas(pasta: Path) -> list[tuple[str, object]]:
    """(pedido, nota) das notas autorizadas e conferidas (cliente, valor, boleto) de um lote de produção."""
    if not (pasta / "notas").exists():
        return []
    _, remessa, ambiente = carregar(pasta.name)
    notas, _ = notas_do_lote(pasta)
    serie = serie_do_lote(pasta)
    return [(r.venda.pedido, r.nota) for r in associar(remessa, notas, registro_de(ambiente, serie), serie) if r.apta]


def entregar_retorno(itens: list[tuple[Path, tuple]], pasta_destino: Path, modo: str, descricao: str, voltar: str):
    """Gera o arquivo de retorno com os (lote, (pedido, nota)) informados, guarda no lote e na pasta do
    Gerência, marca as notas como já retornadas e devolve o arquivo para download."""
    if not itens:
        flash("Nenhuma nota para o retorno: " + ("todas as notas conferidas já foram em retornos anteriores."
                                                  if modo == "novas" else "não há notas autorizadas e conferidas."), "erro")
        return redirect(voltar)
    try:
        nome, conteudo = gerar_retorno([par for _, par in itens], datetime.now(FUSO_BRASILIA))
    except RetornoNaoConfigurado as erro:
        flash(str(erro), "erro")
        return redirect(voltar)
    (pasta_destino / "retorno").mkdir(exist_ok=True)
    (pasta_destino / "retorno" / nome).write_bytes(conteudo)
    try:  # cópia na pasta de onde a equipe importa no Gerência
        config.PASTA_RETORNO_GERENCIA.mkdir(parents=True, exist_ok=True)
        (config.PASTA_RETORNO_GERENCIA / nome).write_bytes(conteudo)
    except OSError:
        pass  # pasta indisponível: o download continua funcionando
    por_lote: dict[Path, list[str]] = {}
    for pasta, (_, nota) in itens:
        por_lote.setdefault(pasta, []).append(nota.numero)
    for pasta, numeros in por_lote.items():
        registrar_retorno(pasta, nome, numeros, modo)
    auditar(f"gerou o retorno {nome} ({len(itens)} notas, {descricao}, modo {modo})")
    return send_file(io.BytesIO(conteudo), mimetype="text/plain", as_attachment=True, download_name=nome)


@app.post("/lote/<lote_id>/retorno")
def baixar_retorno(lote_id):
    """Retorno de um lote de produção: modo "novas" (só notas ainda não enviadas ao Gerência) ou "todas"."""
    pasta = pasta_lote(lote_id)
    modo = "todas" if request.form.get("modo") == "todas" else "novas"
    ja = ja_retornadas(pasta) if modo == "novas" else set()
    itens = [(pasta, par) for par in notas_aptas(pasta) if par[1].numero not in ja]
    return entregar_retorno(itens, pasta, modo, f"lote {lote_id}", url_for("lote", lote_id=lote_id) + "#retorno")


@app.post("/lote/<lote_id>/retorno-remessa")
def baixar_retorno_remessa(lote_id):
    """Retorno da remessa inteira: junta as notas de todos os lotes de produção criados desta remessa."""
    pasta = pasta_lote(lote_id)
    modo = "todas" if request.form.get("modo") == "todas" else "novas"
    itens = []
    for filho in filhos_de(lote_id):
        ja = ja_retornadas(filho) if modo == "novas" else set()
        itens += [(filho, par) for par in notas_aptas(filho) if par[1].numero not in ja]
    return entregar_retorno(itens, pasta, modo, f"remessa inteira do lote {lote_id}",
                            url_for("lote", lote_id=lote_id) + "#retorno")


if __name__ == "__main__":
    print(f"Emissor NFS-e no ar em http://{config.HOST}:{config.PORTA} (feche esta janela para desligar).")
    try:
        from waitress import serve  # servidor para vários usuários ao mesmo tempo

        serve(app, host=config.HOST, port=config.PORTA, threads=8)
    except ImportError:
        app.run(host=config.HOST, port=config.PORTA, debug=False)
