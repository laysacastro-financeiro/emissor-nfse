"""Histórico de CEPs aceitos/recusados pelo ISS Net, lido dos registros de comunicação."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nfse.historico_cep import HistoricoCep  # noqa: E402


class TestHistoricoCep(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.dia = self.base / "comunicacao" / "2026-10-30"
        self.dia.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def registrar(self, nome, ibge, cep, resposta):
        (self.dia / f"{nome}_GerarNfse_envio.xml").write_text(
            f"<toma><end><endNac><cMun>{ibge}</cMun><CEP>{cep}</CEP></endNac></end></toma>", "utf-8")
        if resposta is not None:
            (self.dia / f"{nome}_GerarNfse_resposta.xml").write_text(resposta, "utf-8")

    def historico(self):
        return HistoricoCep(self.base / "comunicacao", self.base / "historico.json")

    def test_cruza_o_cep_enviado_com_a_resposta_do_iss_net(self):
        self.registrar("100000_1", "1505536", "68310400", "<Codigo>E0240</Codigo>")
        self.registrar("100001_2", "1505536", "68515000", "<nNFSe>77</nNFSe>")
        self.registrar("100002_3", "1505536", "68515000", "<nNFSe>78</nNFSe>")
        self.registrar("100003_4", "1505536", "68600000", "<Codigo>E232</Codigo>")  # erro que não é de CEP: ignora
        h = self.historico()
        h.atualizar()
        self.assertEqual(h.recusado("1505536", "68310400"), 1)
        self.assertEqual(h.recusado("1505536", "68515000"), 0)
        self.assertEqual(h.resultado("1505536", "68600000"), {})
        self.assertEqual(h.aceitos_do_municipio("1505536"), [{"cep": "68515000", "aceitos": 2}])

    def test_so_a_ultima_resposta_vale_e_o_geral_da_cidade_vem_primeiro(self):
        self.registrar("100000_1", "3204609", "29650000", "<Codigo>E0240</Codigo>")
        self.registrar("100001_2", "3204609", "29650000", "<nNFSe>1</nNFSe>")  # passou depois: não é mais recusado
        self.registrar("100002_3", "3204609", "29650348", "<nNFSe>2</nNFSe>")
        self.registrar("100003_4", "3204609", "29650349", "<nNFSe>3</nNFSe>")
        self.registrar("100004_5", "3204609", "29650349", "<nNFSe>4</nNFSe>")
        h = self.historico()
        h.atualizar()
        self.assertEqual(h.recusado("3204609", "29650000"), 0)
        self.assertEqual([a["cep"] for a in h.aceitos_do_municipio("3204609")], ["29650000", "29650349", "29650348"])

    def test_le_so_o_que_e_novo_e_espera_a_resposta_que_ainda_nao_chegou(self):
        self.registrar("100000_1", "1000001", "11111000", "<Codigo>E0240</Codigo>")
        self.registrar("100001_2", "1000001", "22222000", None)  # envio sem resposta ainda
        h = self.historico()
        h.atualizar()
        self.assertEqual((h.recusado("1000001", "11111000"), h.resultado("1000001", "22222000")), (1, {}))
        (self.dia / "100001_2_GerarNfse_resposta.xml").write_text("<nNFSe>1</nNFSe>", "utf-8")
        reaberto = self.historico()  # outro processo: parte do arquivo salvo
        reaberto.atualizar()
        self.assertEqual(reaberto.recusado("1000001", "11111000"), 1)  # não contou duas vezes
        self.assertEqual(reaberto.aceitos_do_municipio("1000001"), [{"cep": "22222000", "aceitos": 1}])


if __name__ == "__main__":
    unittest.main()

