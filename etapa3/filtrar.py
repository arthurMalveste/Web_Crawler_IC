"""Etapa 3 — filtro por palavras-chave sobre o texto completo.

Le os textos da Etapa 2 (<corpus>/texto/indice.jsonl) e da a cada documento
duas notas independentes, com os termos de config/lexicon.yaml:

- ConOps: strong_terms, moderate_terms e as secoes canonicas
  (structural_signature) encontradas como titulo de secao;
- dominio: domain_terms (aeronautica + espacial + seguranca/STPA).

Cada nota tem seu limiar -> faixa: ambos, so_conops, so_dominio, nenhum.
Nao descarta nada e nao julga se o documento e um ConOps (isso e a Etapa 5):
so registra a faixa e a evidencia em <corpus>/filtro.jsonl.

    python etapa3\\filtrar.py
"""

import collections
import json
import os
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from crawler.core.prefilter import normalize  # mesma normalizacao da Etapa 1

CORPUS = (REPO / os.environ.get("CONOPS_DATA_ROOT", "../ConOpsCorpus")).resolve()
FAIXAS = ["ambos", "so_conops", "so_dominio", "nenhum"]


def contar(texto: str, termos: list[str]) -> dict[str, int]:
    """Ocorrencias de cada termo no texto ja normalizado (limite de palavra)."""
    achados = {t: texto.count(f" {t} ") for t in termos}
    return {t: n for t, n in achados.items() if n}


def titulos(bruto: str) -> list[str]:
    """Titulos de secao: linhas "#" do Docling; no .txt da NASA (sem marcacao),
    linhas curtas, que podem ser um titulo solto. Numeracao ("2.3.") removida."""
    linhas = [l.lstrip("#") for l in bruto.splitlines() if l.startswith("#") or len(l) < 80]
    return [normalize(re.sub(r"^[\s\d.]+", "", l)) for l in linhas]


def secoes_canonicas(bruto: str, assinatura: list[str]) -> list[str]:
    ts = [t for t in titulos(bruto) if t]
    # o titulo tem de ser a secao canonica (com no maximo umas palavras a mais),
    # nao uma frase longa que so a menciona
    return [s for s in assinatura if any(s in t and len(t) <= len(s) + 25 for t in ts)]


def nota(ocorr_forte: dict, ocorr_mod: dict, palavras: int, cfg: dict) -> float:
    pontos = cfg["peso_forte"] * sum(ocorr_forte.values()) + cfg["peso_moderado"] * sum(ocorr_mod.values())
    return pontos * 10_000 / max(palavras, 1)


def main() -> None:
    lx = yaml.safe_load((REPO / "config" / "lexicon.yaml").read_text(encoding="utf-8"))
    cfg = lx["etapa3"]
    norm = lambda chave, d=lx: [normalize(t) for t in d[chave]]
    c_forte, c_mod, assinatura = norm("strong_terms"), norm("moderate_terms"), norm("structural_signature")
    d_forte, d_mod = norm("strong", lx["domain_terms"]), norm("moderate", lx["domain_terms"])

    indice = [json.loads(l) for l in (CORPUS / "texto" / "indice.jsonl").read_text(encoding="utf-8").splitlines()]
    saida = []
    for doc in indice:
        bruto = (CORPUS / "texto" / f"{doc['sha256']}.txt").read_text(encoding="utf-8")
        texto = f" {normalize(bruto)} "
        palavras = len(texto.split())

        cf, cm, df, dm = (contar(texto, t) for t in (c_forte, c_mod, d_forte, d_mod))
        secoes = secoes_canonicas(bruto, assinatura)
        no_titulo = bool(contar(f" {normalize(doc['title'])} ", c_forte))
        n_conops = (nota(cf, cm, palavras, cfg) + cfg["peso_estrutural"] * len(secoes)
                    + cfg["peso_titulo_conops"] * no_titulo)
        n_dominio = nota(df, dm, palavras, cfg)

        e_conops = n_conops >= cfg["limiar_conops"]
        e_dominio = n_dominio >= cfg["limiar_dominio"] and sum(df.values()) >= cfg["min_fortes_dominio"]
        faixa = FAIXAS[0 if e_conops and e_dominio else 1 if e_conops else 2 if e_dominio else 3]
        mais = lambda *ds: [t for t, _ in collections.Counter({k: v for d in ds for k, v in d.items()}).most_common(8)]
        saida.append({
            "sha256": doc["sha256"], "source": doc["source"], "title": doc["title"], "origem": doc["origem"],
            "palavras": palavras, "faixa": faixa,
            "nota_conops": round(n_conops, 1), "nota_dominio": round(n_dominio, 1),
            "termos_conops": mais(cf, cm), "secoes_canonicas": secoes, "termos_dominio": mais(df, dm),
        })

    with (CORPUS / "filtro.jsonl").open("w", encoding="utf-8") as f:
        for r in saida:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"{len(saida)} documentos  (limiares: ConOps >= {cfg['limiar_conops']}, dominio >= {cfg['limiar_dominio']})\n")
    fontes = sorted({r["source"] for r in saida})
    print(f"{'faixa':12}{'total':>6}  " + "".join(f"{s:>12}" for s in fontes))
    for fx in FAIXAS:
        linha = [r for r in saida if r["faixa"] == fx]
        print(f"{fx:12}{len(linha):6}  " + "".join(f"{sum(r['source'] == s for r in linha):12}" for s in fontes))


if __name__ == "__main__":
    main()
