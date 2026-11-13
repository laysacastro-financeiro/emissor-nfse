"""Verificação de CEP x município do tomador e busca do CEP correto pelo endereço.

O ISS Net recusa a nota (E0240) quando o CEP do tomador não existe ou não pertence ao município do
cadastro. Aqui o CEP é conferido ANTES de emitir, em fontes públicas:
- ViaCEP (viacep.com.br): dá o município (código IBGE) de cada CEP e busca CEPs por UF + cidade + rua;
- IBGE (servicodados.ibge.gov.br): nome do município a partir do código IBGE.

Só saem do computador o CEP e, para sugerir o correto, UF, cidade e rua do tomador (dados de empresas).
Os resultados ficam em cache (dados/cache_cep.json) para não repetir consultas.
"""
from __future__ import annotations

import difflib
import gzip
import json
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import config

ARQUIVO_CACHE = config.DADOS_DIR / "cache_cep.json"
VALIDADE_ENCONTRADO = 90 * 86400  # CEP encontrado: confere de novo só depois de 90 dias
VALIDADE_NAO_ENCONTRADO = 7 * 86400  # CEP não encontrado: tenta de novo em 7 dias (as bases se atualizam)
INTERVALO_ENTRE_CONSULTAS = 0.15  # segundos; respeita o limite de uso do ViaCEP
TIPOS_LOGRADOURO = (
    "rua", "r", "avenida", "av", "travessa", "tv", "alameda", "al", "rodovia", "rod", "estrada", "est", "praca",
    "pca", "largo", "via", "vila", "beco", "quadra", "qd", "lote", "setor", "sqs", "sqn", "sia", "sql", "scs",
)
_trava = threading.Lock()


@dataclass
class ResultadoCep:
    """status: ok | geral (ok, mas é o CEP geral do município e há CEP da rua) | divergente | nao_encontrado |
    indisponivel."""

    status: str
    cidade_do_cep: str = ""
    ibge_do_cep: str = ""
    sugestoes: list[dict] = field(default_factory=list)
    # Preenchido quando rua e bairro do cadastro existem no município do CEP (o município do cadastro é que
    # parece errado): {"ibge", "cidade", "uf", "cep", "logradouro", "bairro"}.
    alternativa_municipio: dict = field(default_factory=dict)
    # Endereço cadastrado do CNPJ na Receita Federal (vazio se CPF, CNPJ fora da base ou serviço indisponível).
    receita: dict = field(default_factory=dict)
    receita_consultada: bool = False
    # CEP da Receita que confere com o cadastro em tudo (município, rua) e existe nos Correios: a nota pode
    # sair com ele sem a equipe precisar abrir o cartão CNPJ.
    cep_automatico: str = ""
    # O endereço do CNPJ na Receita fica em OUTRO município que o do cadastro (e o CEP dele existe nos
    # Correios): {"cep", "ibge", "cidade", "uf", "logradouro", "numero", "bairro"}. Só a equipe aplica.
    endereco_receita: dict = field(default_factory=dict)
    # O CEP do cadastro é o geral do município (os Correios não detalham rua).
    cep_geral: bool = False
    # As sugestões por nome de rua (ViaCEP, lento) ainda não foram buscadas: o status já vale, as sugestões vêm depois.
    ruas_pendentes: bool = False

    def como_dict(self) -> dict:
        return {"status": self.status, "cidade_do_cep": self.cidade_do_cep, "ibge_do_cep": self.ibge_do_cep,
                "sugestoes": self.sugestoes, "alternativa_municipio": self.alternativa_municipio,
                "receita": self.receita, "receita_consultada": self.receita_consultada,
                "cep_automatico": self.cep_automatico, "endereco_receita": self.endereco_receita,
                "cep_geral": self.cep_geral, "ruas_pendentes": self.ruas_pendentes}


def sem_acentos(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))


def normalizar_cep(cep: str) -> str:
    return re.sub(r"\D", "", cep or "").zfill(8)[:8]


def formatar_cep(cep: str) -> str:
    cep = normalizar_cep(cep)
    return f"{cep[:5]}-{cep[5:]}"


COMPLEMENTOS = r"loja|sala|sl|andar|bloco|bl|apto|ap|apartamento|conjunto|cj|galpao|km|lote|lt|quadra|qd|cxpst|n"
SIMILARIDADE_RUA = 0.75  # parecença mínima (0 a 1) entre a rua do cadastro e a rua achada
SIMILARIDADE_MUNICIPIO = 0.7  # rua e bairro do cadastro "batem" com os do CEP de outra cidade


def nome_da_rua(logradouro: str) -> str:
    """Só o nome da rua: sem acentos, minúsculo, sem número/complemento e sem o tipo (Rua, Av, Travessa...).

    'R ARISTOBULO BARBOSA LEAO 500 LOJA 27 - MATA DA PRAIA - VITO' -> 'aristobulo barbosa leao'."""
    texto = re.sub(r"\(.*?\)", " ", sem_acentos(logradouro or "")).lower()
    corte = re.search(rf"(\d|\s-\s|,|\b({COMPLEMENTOS})\b\.?\s*\d*)", texto)
    if corte:
        texto = texto[: corte.start()]
    palavras = re.sub(r"[^a-z ]", " ", texto).split()
    if len(palavras) > 1 and palavras[0] in TIPOS_LOGRADOURO:
        palavras = palavras[1:]
    return " ".join(palavras)


def parecenca(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, nome_da_rua(a) or a.lower(), nome_da_rua(b) or b.lower()).ratio()


def termos_de_busca(logradouro: str) -> list[str]:
    """Variações do logradouro para a busca por endereço, da mais completa para a mais genérica:
    com o tipo (Rua Sao Bento), só o nome (Sao Bento) e a primeira palavra do nome (para erros de digitação)."""
    texto = re.sub(r"\(.*?\)", " ", sem_acentos(logradouro or ""))
    corte = re.search(rf"(\d|\s-\s|,|\b({COMPLEMENTOS})\b\.?\s*\d*)", texto, flags=re.IGNORECASE)
    if corte:
        texto = texto[: corte.start()]
    palavras = re.sub(r"[^A-Za-z ]", " ", texto).split()
    completo = " ".join(palavras)
    if len(palavras) > 1 and palavras[0].lower() in TIPOS_LOGRADOURO:
        palavras = palavras[1:]
    nome = " ".join(palavras)
    termos = []
    for termo in (completo, nome, palavras[0] if palavras else ""):
        if len(termo) >= 3 and termo.lower() not in [t.lower() for t in termos]:
            termos.append(termo)
    return termos


def buscar_json(url: str):
    """GET de uma API pública que devolve JSON (aceita resposta compactada em gzip)."""
    pedido = urllib.request.Request(url, headers={"User-Agent": "EmissorNFSe/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(pedido, timeout=12) as resposta:
        corpo = resposta.read()
    if corpo[:2] == b"\x1f\x8b":  # alguns serviços (IBGE) devolvem gzip mesmo sem Accept-Encoding
        corpo = gzip.decompress(corpo)
    return json.loads(corpo.decode("utf-8"))


class ServicoCep:
    def __init__(self, buscar=None, cache: Path | None = None, agora=time.time, cnpj=None, rapido: bool | None = None):
        self._buscar = buscar or buscar_json
        self._arquivo = Path(cache or ARQUIVO_CACHE)
        self._agora = agora
        self.consultas_feitas = 0
        self.cnpj = cnpj  # ServicoCnpj: consulta o endereço cadastrado na Receita Federal (opcional)
        self._memoria: dict | None = None  # cache em memória: o arquivo só é lido uma vez
        # BrasilAPI (resposta em ~0,1 s, agrega Correios/ViaCEP/OpenCEP) como 1ª fonte do CEP; o ViaCEP, que
        # costuma ficar lento sob uso intenso, vira reserva. Só com a rede de verdade (nos testes, buscar é falso).
        self._rapido = (buscar is None) if rapido is None else rapido

    # -- rede e cache ---------------------------------------------------------------------------------------
    @staticmethod
    def _buscar_http(url: str):
        return buscar_json(url)

    def _ler_cache(self) -> dict:
        with _trava:
            if self._memoria is None:
                self._memoria = json.loads(self._arquivo.read_text("utf-8")) if self._arquivo.exists() else {}
            return self._memoria

    def _gravar_cache(self, chave: str, valor: dict) -> None:
        cache = self._ler_cache()
        with _trava:
            cache[chave] = {**valor, "em": self._agora()}
            self._arquivo.parent.mkdir(parents=True, exist_ok=True)
            temporario = self._arquivo.with_suffix(".tmp")
            temporario.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
            temporario.replace(self._arquivo)

    def _do_cache(self, chave: str, validade: float | None = None):
        item = self._ler_cache().get(chave)
        if not item:
            return None
        if validade is not None and self._agora() - item["em"] > validade:
            return None
        return item

    def _consultar(self, url: str, pausa: bool = True):
        self.consultas_feitas += 1
        if pausa:
            time.sleep(INTERVALO_ENTRE_CONSULTAS)
        return self._buscar(url)

    # -- consultas ------------------------------------------------------------------------------------------
    def _cep_no_cache(self, cep: str):
        """Dados guardados do CEP ({} = não existe), ou None se não há registro válido."""
        item = self._do_cache(f"cep:{cep}")
        if item is not None and (
            self._agora() - item["em"] <= (VALIDADE_ENCONTRADO if item.get("achou") else VALIDADE_NAO_ENCONTRADO)
        ):
            return item["dados"]
        return None

    def _cep_brasilapi(self, cep: str) -> dict | None:
        """CEP pela BrasilAPI; None se não achou ou ficou indisponível (aí vale a confirmação do ViaCEP)."""
        try:
            r = self._consultar(f"https://brasilapi.com.br/api/cep/v2/{cep}", pausa=False)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None
        ibge = (r.get("ibge") or {}).get("city") if isinstance(r, dict) else None
        if not ibge or not r.get("city"):
            return None
        return {"ibge": str(ibge), "cidade": r["city"], "uf": r.get("state", ""),
                "logradouro": r.get("street") or "", "bairro": r.get("neighborhood") or ""}

    def pre_carregar(self, ceps, limite_segundos: float = 120, threads: int = 8) -> None:
        """Consulta em paralelo os CEPs que ainda não estão no cache, para a verificação seguinte sair do cache."""
        faltam = sorted({normalizar_cep(c) for c in ceps if len(normalizar_cep(c)) == 8
                         and self._cep_no_cache(normalizar_cep(c)) is None})
        inicio = time.monotonic()
        with ThreadPoolExecutor(threads) as pool:
            for i in range(0, len(faltam), threads * 4):
                if time.monotonic() - inicio >= limite_segundos:
                    break
                list(pool.map(self._dados_do_cep_sem_erro, faltam[i : i + threads * 4]))

    def _dados_do_cep_sem_erro(self, cep: str):
        try:
            return self.dados_do_cep(cep)
        except Exception:  # um CEP com problema não derruba o lote: a verificação normal tenta de novo
            return None

    def dados_do_cep(self, cep: str) -> dict | None:
        """{'ibge', 'cidade', 'uf', 'logradouro', 'bairro'} do CEP, {} se não existir, None se indisponível."""
        cep = normalizar_cep(cep)
        guardado = self._cep_no_cache(cep)
        if guardado is not None:
            return guardado
        if self._rapido:
            dados = self._cep_brasilapi(cep)
            if dados:
                self._gravar_cache(f"cep:{cep}", {"achou": True, "dados": dados})
                return dados
        try:
            resposta = self._consultar(f"https://viacep.com.br/ws/{cep}/json/")
        except urllib.error.HTTPError as erro:
            if erro.code == 400:  # formato inválido
                resposta = {"erro": True}
            else:
                return None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None
        if not isinstance(resposta, dict) or resposta.get("erro"):
            self._gravar_cache(f"cep:{cep}", {"achou": False, "dados": {}})
            return {}
        dados = {"ibge": str(resposta.get("ibge", "")), "cidade": resposta.get("localidade", ""),
                 "uf": resposta.get("uf", ""), "logradouro": resposta.get("logradouro", ""),
                 "bairro": resposta.get("bairro", "")}
        self._gravar_cache(f"cep:{cep}", {"achou": True, "dados": dados})
        return dados

    def nome_do_municipio(self, ibge: str) -> str:
        item = self._do_cache(f"ibge:{ibge}")
        if item:
            return item["nome"]
        try:
            resposta = self._consultar(f"https://servicodados.ibge.gov.br/api/v1/localidades/municipios/{ibge}")
            nome = resposta["nome"] if isinstance(resposta, dict) else resposta[0]["nome"]
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError):
            return ""
        self._gravar_cache(f"ibge:{ibge}", {"nome": nome})
        return nome

    def sugerir(self, uf: str, ibge: str, logradouro: str, bairro: str = "") -> list[dict]:
        """CEPs da mesma rua no município do cadastro (busca por endereço). Os do mesmo bairro vêm primeiro."""
        cidade = self.nome_do_municipio(ibge)
        if not (uf and cidade):
            return []
        encontrados: dict[str, dict] = {}
        for termo in termos_de_busca(logradouro)[:2]:  # cada busca no ViaCEP leva segundos: só os 2 termos mais completos
            chave = f"sug:{uf}|{ibge}|{termo.lower()}"
            item = self._do_cache(chave, VALIDADE_NAO_ENCONTRADO)
            if item is not None:
                lista = item["lista"]
            else:
                url = ("https://viacep.com.br/ws/" + "/".join(
                    urllib.parse.quote(parte) for parte in (uf, sem_acentos(cidade), termo)) + "/json/")
                try:
                    resposta = self._consultar(url)
                except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                    continue
                lista = resposta if isinstance(resposta, list) else []
                self._gravar_cache(chave, {"lista": lista})
            for r in lista:
                # Só CEP do município do cadastro e de rua parecida com a do cadastro (a busca por termo
                # genérico traz ruas que não têm a ver).
                if str(r.get("ibge", "")) == str(ibge) and r.get("cep") \
                        and parecenca(logradouro, r.get("logradouro", "")) >= SIMILARIDADE_RUA:
                    encontrados[normalizar_cep(r["cep"])] = {
                        "cep": normalizar_cep(r["cep"]), "logradouro": r.get("logradouro", ""),
                        "bairro": r.get("bairro", "")}
            if encontrados:
                break  # o termo mais completo já achou: não precisa dos mais genéricos
        bairro_normal = sem_acentos(bairro or "").lower().strip()
        ordenadas = sorted(encontrados.values(),
                           key=lambda s: (sem_acentos(s["bairro"]).lower().strip() != bairro_normal, s["cep"]))
        return ordenadas[:6]

    @staticmethod
    def municipio_do_cep_confere_com_endereco(dados: dict, logradouro: str, bairro: str) -> bool:
        """O CEP é de OUTRO município, mas a rua e o bairro dele são os do cadastro: provavelmente é o
        município do cadastro que está errado, e não o CEP."""
        if not dados or not dados.get("logradouro"):
            return False
        rua_ok = parecenca(logradouro, dados["logradouro"]) >= SIMILARIDADE_MUNICIPIO
        bairro_a, bairro_b = sem_acentos(bairro or "").lower().strip(), sem_acentos(dados.get("bairro", "")).lower().strip()
        bairro_ok = bool(bairro_a and bairro_b) and difflib.SequenceMatcher(None, bairro_a, bairro_b).ratio() >= SIMILARIDADE_MUNICIPIO
        return rua_ok and bairro_ok

    def _conferir_com_receita(self, resultado: ResultadoCep, documento: str, cep: str, ibge: str,
                              logradouro: str, bairro: str, buscar_ruas: bool = True) -> None:
        """Acrescenta ao resultado o que o cadastro do CNPJ na Receita Federal diz do endereço."""
        if not self.cnpj or len(re.sub(r"\D", "", documento or "")) != 14:
            return
        receita = self.cnpj.consultar(documento)
        if receita is None:  # serviço fora do ar: tenta de novo na próxima verificação
            return
        resultado.receita_consultada = True
        if not receita or not receita.get("cep"):
            return
        resultado.receita = receita
        cep_receita = normalizar_cep(receita["cep"])
        dados_receita = self.dados_do_cep(cep_receita)  # o CEP da Receita precisa existir nos Correios
        mesma_rua = parecenca(logradouro, receita.get("logradouro", "")) >= SIMILARIDADE_RUA
        if not dados_receita:
            pass
        elif dados_receita["ibge"] == str(ibge):
            if cep_receita != normalizar_cep(cep):
                sugestao = {"cep": cep_receita, "logradouro": dados_receita["logradouro"] or receita.get("logradouro", ""),
                            "bairro": dados_receita["bairro"] or receita.get("bairro", ""), "origem": "receita",
                            "mesma_rua": mesma_rua}
                resultado.sugestoes = [sugestao] + [s for s in resultado.sugestoes if s["cep"] != cep_receita]
                if mesma_rua:
                    resultado.cep_automatico = cep_receita
        elif resultado.ibge_do_cep and str(receita.get("ibge")) == resultado.ibge_do_cep:
            # O CEP do cadastro e o endereço da Receita estão no mesmo município, que não é o do cadastro:
            # a Receita confirma que o município do cadastro é que está errado.
            resultado.alternativa_municipio = {
                "ibge": resultado.ibge_do_cep, "cidade": resultado.cidade_do_cep, "uf": receita.get("uf", ""),
                "cep": normalizar_cep(cep), "logradouro": receita.get("logradouro", ""),
                "bairro": receita.get("bairro", ""), "confirmado_receita": True}
        else:
            # O cadastro do Gerência está em outra cidade que o CNPJ na Receita (e o CEP do cadastro não ajuda).
            resultado.endereco_receita = {
                "cep": cep_receita, "ibge": dados_receita["ibge"], "cidade": dados_receita["cidade"],
                "uf": dados_receita["uf"], "logradouro": receita.get("logradouro", ""),
                "numero": receita.get("numero", ""), "bairro": receita.get("bairro", "")}
        self._ruas_da_receita(resultado, receita, dados_receita, ibge, cep, buscar_ruas)

    def _ruas_da_receita(self, resultado: ResultadoCep, receita: dict, dados_receita: dict | None, ibge: str,
                         cep: str, buscar_ruas: bool = True) -> None:
        """O CEP da Receita é geral (a cidade tem um CEP só, ou os Correios não detalham a rua): procura os CEPs
        da rua que a Receita informa, para a equipe escolher o mais próximo."""
        rua, ibge_receita = receita.get("logradouro", ""), str(receita.get("ibge") or "")
        if not rua or not ibge_receita or (dados_receita and dados_receita.get("logradouro")):
            return
        if not buscar_ruas:  # a busca por rua é a parte lenta (ViaCEP): fica para a segunda etapa
            resultado.ruas_pendentes = True
            return
        achados = [s for s in self.sugerir(receita.get("uf", ""), ibge_receita, rua, receita.get("bairro", ""))
                   if s["cep"] != normalizar_cep(cep)]
        if ibge_receita == str(ibge):
            existentes = {s["cep"] for s in resultado.sugestoes}
            resultado.sugestoes += [{**s, "origem": "receita_rua"} for s in achados if s["cep"] not in existentes]
        elif resultado.endereco_receita:
            resultado.endereco_receita["alternativas"] = achados

    def verificar(self, cep: str, ibge: str, uf: str, logradouro: str, bairro: str = "",
                  documento: str = "", buscar_ruas: bool = True) -> ResultadoCep:
        """Confere o CEP do cadastro contra o município do cadastro e, se houver problema, sugere o CEP.
        Com o CNPJ do cliente, consulta também o endereço cadastrado dele na Receita Federal.

        buscar_ruas=False pula a busca de CEPs por nome de rua (lenta, no ViaCEP): o resultado sai com
        ruas_pendentes=True e uma segunda chamada, com buscar_ruas=True, completa as sugestões."""
        dados = self.dados_do_cep(cep)
        if dados is None:
            return ResultadoCep("indisponivel")
        if dados and dados["ibge"] == str(ibge):
            resultado = ResultadoCep("ok", dados["cidade"], dados["ibge"])
            if not dados.get("logradouro"):  # CEP geral do município: a Receita pode indicar a rua
                resultado.cep_geral = True
                self._conferir_com_receita(resultado, documento, cep, ibge, logradouro, bairro, buscar_ruas)
                if resultado.sugestoes:
                    resultado.status = "geral"
            return resultado
        status = "divergente" if dados else "nao_encontrado"
        sugestoes = ([s for s in self.sugerir(uf, ibge, logradouro, bairro) if s["cep"] != normalizar_cep(cep)]
                     if buscar_ruas else [])
        alternativa = {}
        if dados and self.municipio_do_cep_confere_com_endereco(dados, logradouro, bairro):
            alternativa = {"ibge": dados["ibge"], "cidade": dados["cidade"], "uf": dados["uf"],
                           "cep": normalizar_cep(cep), "logradouro": dados["logradouro"], "bairro": dados["bairro"]}
        resultado = ResultadoCep(status, dados.get("cidade", "") if dados else "",
                                 dados.get("ibge", "") if dados else "", sugestoes, alternativa,
                                 ruas_pendentes=not buscar_ruas)
        self._conferir_com_receita(resultado, documento, cep, ibge, logradouro, bairro, buscar_ruas)
        return resultado
