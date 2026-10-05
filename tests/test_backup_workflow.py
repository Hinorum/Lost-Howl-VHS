"""db-backup.yml — единственный контур копий, переживающих деплой.

Рвётся он тихо: воркфлоу может падать два дня подряд, а бот об этом не знает
(ALERT_BACKUP_KEY смотрит только на локальный pg_dump в контейнере Render).
Поэтому инварианты шагов проверяются здесь, а не в момент инцидента.

Инвариант, который здесь защищается: **сырой дамп удаляется только ПОСЛЕ
шифрования**. Раньше `rm -f backup-*.dump` стоял в шаге `pg_dump`, сразу после
самого дампа, — файл исчезал до шифрования, шаг Encrypt находил пустоту,
падал с кодом 2, и артефакт не появлялся. Смысл правки был правильный
(«сырой дамп не должен пережить шифрование»), но стоял не в том шаге.
"""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "db-backup.yml"


def _steps() -> dict[str, str]:
    """Имя шага → его скрипт."""
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return {
        step["name"]: step["run"]
        for step in data["jobs"]["dump"]["steps"]
        if "run" in step
    }


def _code(script: str) -> str:
    """Скрипт без комментариев: только то, что реально выполнится."""
    return "\n".join(
        line for line in script.splitlines() if not line.strip().startswith("#")
    )


def test_raw_dump_is_not_deleted_before_encryption() -> None:
    """Шаг pg_dump не имеет права удалять дамп — он ещё не зашифрован.

    Проверяется именно порядок «удаление только после шифрования», а не
    отсутствие rm как такового: Encrypt удалять обязан, это и есть требование.
    Комментарии не исполняются и потому не считаются: объяснение в шаге про
    `rm -f "${raw}"` из Encrypt не должно выглядеть как нарушение.
    """
    dump = _code(_steps()["pg_dump"])
    assert "rm -f" not in dump, f"шаг pg_dump удаляет дамп до шифрования:\n{dump}"
    assert "test -s" in dump, "pg_dump должен убедиться, что дамп непустой"


def test_encrypt_removes_plaintext_afterwards() -> None:
    """Шаг Encrypt шифрует и только потом снимает сырой файл с диска."""
    encrypt = _code(_steps()["Encrypt"])
    enc_at = encrypt.index("openssl enc")
    rm_at = encrypt.index('rm -f "${raw}"')
    assert enc_at < rm_at, "сырой дамп удаляется раньше, чем зашифрован"
    # И страховка от тихой ошибки: «зашифровал не тот файл» не должно выглядеть
    # как успех, поэтому остатки незашифрованных дампов — ошибка.
    assert "backup-*.dump" in encrypt and "::error::" in encrypt


def test_missing_passphrase_fails_loudly() -> None:
    """Без ключа шифрования прогон падает, а не публикует открытый дамп."""
    steps = _steps()
    check = steps["Check encryption key is configured"]
    assert "::error::" in check and "exit 1" in check
    # Ключ читается в обоих шагах: проверка без него бессмысленна.
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in data["jobs"]["dump"]["steps"]:
        if step.get("name") in ("Check encryption key is configured", "Encrypt"):
            assert step["env"]["BACKUP_PASSPHRASE"].startswith("${{ secrets.")


def test_artifact_carries_only_ciphertext() -> None:
    """В артефакт уезжает только .enc — расширение специально для этого."""
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    upload = next(
        step
        for step in data["jobs"]["dump"]["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact")
    )
    assert upload["with"]["path"].strip() == "backup-*.enc"
    assert upload["with"]["if-no-files-found"] == "error"
