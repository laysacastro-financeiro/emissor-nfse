"""Histórico dos CEPs que o ISS Net aceitou ou recusou (E0240), lido dos registros de comunicação.

A base de CEPs do ISS Net não é igual à dos Correios: um CEP que existe nos Correios pode ser recusado para o
município. Cada GerarNfse fica gravado em dados/comunicacao/<dia>/<hora>_<n>_GerarNfse_envio.xml (a DPS enviada,
com município e CEP do tomador) e _resposta.xml (a nota ou o erro). Cruzando os dois sabemos, por município e
CEP, o que o ISS Net já aceitou ou recusou, inclusive as tentativas que depois foram corrigidas."""
import json
import re
import threading
import time
from pathlib import Path

ENDERECO = re.compile(rb"<endNac>\s*<cMun>(\d{7})</cMun>\s*<CEP>(\d{8})</CEP>")
SUFIXO_ENVIO = "_GerarNfse_envio.xml"
SUFIXO_RESPOSTA = "_GerarNfse_resposta.xml"
ACEITO, RECUSADO = "A", "R"


def classificar(resposta: bytes) -> str:
    """'A' se o ISS Net gerou a nota, 'R' se recusou o CEP (E0240), '' para qualquer outra coisa."""
    if b"E0240" in resposta:
        return RECUSADO
    if b"<nNFSe>" in resposta:
        return ACEITO
    return ""


class HistoricoCep:
    def __init__(self, pasta_comunicacao: Path, arquivo: Path):
        self.pasta = Path(pasta_comunicacao)
        self.arquivo = Path(arquivo)
        self._trava = threading.Lock()
        self._ceps: dict[str, dict] = {}
        self._dias: dict[str, str] = {}
        if self.arquivo.exists():
            try:
                dados = json.loads(self.arquivo.read_text("utf-8"))
                self._ceps, self._dias = dados.get("ceps", {}), dados.get("dias", {})
            except (OSError, ValueError):
                pass

    def atualizar(self, limite_segundos: float = 20.0) -> bool:
        """Lê só os registros novos. Devolve False quando outra atualização já está em andamento."""
        if not self._trava.acquire(blocking=False):
            return False
        try:
            if not self.pasta.is_dir():
                return True
            inicio, mudou = time.monotonic(), False
            for dia in sorted(p for p in self.pasta.iterdir() if p.is_dir()):
                ultimo = self._dias.get(dia.name, "")
                for envio in sorted(p for p in dia.iterdir() if p.name.endswith(SUFIXO_ENVIO)):
                    if envio.name <= ultimo:
                        continue
                    if time.monotonic() - inicio > limite_segundos:
                        if mudou:
                            self._gravar()
                        return True
                    resposta = envio.with_name(envio.name[: -len(SUFIXO_ENVIO)] + SUFIXO_RESPOSTA)
                    if not resposta.exists():
                        break  # envio ainda sem resposta: volta nele na próxima
                    self._registrar(envio.read_bytes(), resposta.read_bytes(), dia.name)
                    self._dias[dia.name] = envio.name
                    mudou = True
            if mudou:
                self._gravar()
            return True
        finally:
            self._trava.release()

    def _registrar(self, envio: bytes, resposta: bytes, dia: str) -> None:
        resultado, endereco = classificar(resposta), ENDERECO.search(envio)
        if not resultado or not endereco:
            return
        ibge, cep = endereco.group(1).decode(), endereco.group(2).decode()
        item = self._ceps.setdefault(f"{ibge}|{cep}", {"aceitos": 0, "recusados": 0})
        item["aceitos" if resultado == ACEITO else "recusados"] += 1
        item["ultimo"], item["dia"] = resultado, dia

    def _gravar(self) -> None:
        self.arquivo.parent.mkdir(parents=True, exist_ok=True)
        temporario = self.arquivo.with_suffix(".tmp")
        temporario.write_text(json.dumps({"dias": self._dias, "ceps": self._ceps}, ensure_ascii=False), "utf-8")
        temporario.replace(self.arquivo)

    def resultado(self, ibge: str, cep: str) -> dict:
        return dict(self._ceps.get(f"{ibge}|{cep}", {}))

    def recusado(self, ibge: str, cep: str) -> int:
        """Quantas vezes o ISS Net recusou o CEP neste município, se a última resposta foi uma recusa (0 = não)."""
        item = self._ceps.get(f"{ibge}|{cep}", {})
        return item.get("recusados", 0) if item.get("ultimo") == RECUSADO else 0

    def aceitos_do_municipio(self, ibge: str, limite: int = 3) -> list[dict]:
        """CEPs que o ISS Net já aceitou no município (última resposta foi aceite): o geral da cidade primeiro,
        depois os mais usados."""
        achados = [{"cep": chave.split("|")[1], "aceitos": item["aceitos"]}
                   for chave, item in self._ceps.items()
                   if chave.startswith(f"{ibge}|") and item.get("ultimo") == ACEITO]
        achados.sort(key=lambda a: (not a["cep"].endswith("000"), -a["aceitos"], a["cep"]))
        return achados[:limite]
