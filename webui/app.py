"""Painel web local — checkboxes de fonte + métricas ao vivo sobre
`discover`/`harvest`.

Camada fina de propósito: só rotas HTTP. Toda leitura/orquestração vive em
`metrics.py`/`jobs.py`, que por sua vez só chamam a CLI já existente como
subprocesso e leem o mesmo SQLite que o coletor já usa — nada em
`crawler/core`, `crawler/engine` ou `crawler/adapters` muda por causa deste
módulo.

Rodar: `python -m webui.app` (ver webui/README.md).
"""

from __future__ import annotations

from flask import Flask, jsonify, request, send_from_directory

from . import jobs, metrics

app = Flask(__name__, static_folder="static", static_url_path="")


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/sources")
def api_sources():
    return jsonify(metrics.list_sources())


@app.get("/api/status")
def api_status():
    return jsonify(jobs.status())


@app.get("/api/harvest-preview")
def api_harvest_preview():
    """Quantos documentos SERIAM baixados agora, por fonte, para as faixas
    marcadas — mesma consulta que o harvest de fato usaria (ver
    `metrics.pending_by_source`), não uma estimativa."""
    tiers = request.args.getlist("tiers") or None
    por_fonte = metrics.pending_by_source(metrics.data_root(), tiers)
    return jsonify({"total": sum(por_fonte.values()), "por_fonte": por_fonte})


@app.post("/api/discover")
def api_discover():
    body = request.get_json(force=True, silent=True) or {}
    try:
        job_id = jobs.start_discover(body.get("sources") or [], body.get("limit") or None)
    except jobs.JobEmAndamento as exc:
        return jsonify({"erro": str(exc)}), 409
    except ValueError as exc:
        return jsonify({"erro": str(exc)}), 400
    return jsonify({"job_id": job_id})


@app.post("/api/harvest")
def api_harvest():
    body = request.get_json(force=True, silent=True) or {}
    try:
        job_id = jobs.start_harvest(body.get("limit") or None, body.get("tiers") or None)
    except jobs.JobEmAndamento as exc:
        return jsonify({"erro": str(exc)}), 409
    return jsonify({"job_id": job_id})


def main() -> None:
    # Só localhost: esta ferramenta dispara tráfego real contra servidores de
    # produção a partir de um clique — nunca deve ficar acessível na rede.
    app.run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    main()
