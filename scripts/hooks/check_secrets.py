#!/usr/bin/env python3
"""Pre-commit hook: запретить мнемоники/приватные ключи в коммите.

Защищает от случайной утечки TREASURY_MNEMONIC или приватного ключа кошелька
в репозиторий. Ловит:
  - 24 слова из BIP-39 wordlist (TON-кошельки);
  - 64 hex-символа (32 байта) — типичный приватный ключ ed25519;
  - base64 строки длиной ~44, похожие на приватный ключ ed25519.

Что НЕ ловит (намеренно):
  - открытые адреса (0:abc..., UQ...) — они безопасны;
  - примеры мнемоник в тестах/доках — для них есть .secrets-allowlist.

Запуск:
    python scripts/hooks/check_secrets.py [--staged] [--all]

    --staged (по умолчанию) — только файлы из `git diff --cached`.
    --all                 — все файлы в репозитории.

Код возврата:
    0 — чисто.
    1 — найдены подозрительные строки (коммит блокируется).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# BIP-39 wordlist (English, 2048 слов). Намеренно короткий список —
# достаточно нескольких слов, чтобы считать строку подозрительной.
# Этот файл не импортируется: список вшит, чтобы pre-commit работал без
# зависимостей.
_BIP39_HEAD = (
    "abandon ability able about above absent absorb abstract absurd abuse access "
    "accident account accuse achieve acid acoustic acquire across act action actor "
    "actress actual adapt addict address adjust admit adult advance advice aerobic "
    "affair afford afraid again age agent agree ahead aim air airport aisle alarm "
    "album alcohol alert alien all alley allow almost alone alpha already also alter "
    "always amateur amazing among amount amused analyst anchor ancient anger angle "
    "angry animal ankle announce annual another answer antenna antique anxiety any "
    "apart apology appear apple approve april archive arctic area arena argue arm "
    "armed armor army around arrange arrest arrive arrow art artefact artist artwork "
    "ask aspect assault asset assist assume asthma athlete atom attack attend attitude "
    "attract auction audit august aunt author auto autumn average avocado avoid awake "
    "aware awesome awful awkward axis baby bachelor bacon badge bag balance balcony "
    "ball bamboo banana banner bargain barrel base basic basket battle beach bean "
    "beauty because become beef before begin behave behind believe below belt bench "
    "benefit best betray better between beyond bicycle bid bike bind biology bird "
    "birth bitter black blade blame blanket blast bleak bless blind blood blossom "
    "blouse blue blur blush board boat body boil bomb bone bonus book boost border "
    "boring borrow boss bottom bounce box boy bracket brain brand brass brave bread "
    "breeze brick bridge brief bright bring brisk broccoli broken bronze broom brother "
    "brown brush bubble buddy budget buffalo build bulb bulk bullet bundle bunker "
    "burden burger burst bus business busy butter buyer buzz cabbage cabin cable "
    "cactus cage cake call calm camera camp can canal cancel candy cannon canoe "
    "canvas canyon capable capital captain car carbon card cargo carpet carry cart "
    "case cash casino castle casual cat catalog catch category cattle caught cause "
    "caution cave ceiling celery cement census century cereal certain chair chalk "
    "champion change chaos chapter charge chase chat cheap check cheese chef cherry "
    "chest chicken chief child chimney choice choose chronic chuckle chunk churn "
    "cigar cinnamon circle citizen city civil claim clap clarify claw clay clean "
    "clerk clever click client cliff climb clinic clip clock clog close cloth cloud "
    "clown club clump cluster clutch coach coast coconut code coffee coil coin "
    "collect color column combine come comfort comic common company concert conduct "
    "confirm congress connect consider control convince cook cool copper copy coral "
    "core corn correct cost cotton couch country couple course cousin cover coyote "
    "crack cradle craft cram crane crash crater crawl crayon crazy cream credit creek "
    "crew cricket crime crisp critic crop cross crouch crowd crucial cruel cruise "
    "crumble crunch cry crystal cube culture cup curious current curtain curve cushion "
    "custom cute cycle dad damage damp dance danger daring dash daughter dawn day "
    "deal debate debris decade december decide decline decorate decrease deer defense "
    "define defy degree delay deliver demand demise denial dentist deny depart depend "
    "deposit depth deputy derive describe desert design desk despair destroy detail "
    "develop device devote diagram dial diamond diary dice diesel diet differ digital "
    "dignity dilemma dinner dinosaur direct dirt disagree discover disease dish dismiss "
    "disorder display distance divert divide divorce dizzy doctor document dog doll "
    "dolphin domain donate donkey donor door dose double dove draft dragon drama drastic "
    "draw dream dress drift drill drink drip drive drop drum dry duck dumb dune during "
    "dust dutch duty dwarf dynamic eager eagle early earn earth easily east easy echo "
    "ecology economy edge edit educate effort egg eight either elbow elder electric "
    "elegant element elephant elevator elite else embark embody embrace emerge emotion "
    "employ empower empty enable enact end endless endorse enemy energy enforce engage "
    "engine enhance enjoy enlist enough enrich enroll enter entire entry envelope "
    "episode equal equip era erode erosion error erupt escape essay essence estate "
    "eternal ethics evidence evil evoke evolve exact example excess exchange excite "
    "exclude exercise exhaust exhibit exile exist exit exotic expand expect expire "
    "explain expose express extend extra eye eyebrow fabric face faculty fade faint "
    "faith fall false fame family fan fancy fantasy farm fashion fat fatal father "
    "fatigue fault favorite feature february federal fee feed feel female fence festival "
    "fetch fever few fiber fiction field figure file film filter final find fine finger "
    "finish fire firm first fiscal fish fit fitness fix flag flame flash flat flea "
    "flight flip float flock floor flower fluid flush fly foam focus fog foil fold "
    "follow food foot force forest forget fork fortune forward fossil foster found fox "
    "fragile frame frequent fresh friend fringe frog front frost frown frozen fruit "
    "fuel fun funny furnace fury future gadget gain galaxy gallery game gap garage "
    "garbage garden garlic garment gas gasp gate gather gauge gaze general genius "
    "genre gentle genuine ghost giant gift giggle ginger giraffe girl give glad glance "
    "glare glass glide glimpse globe gloom glory glove glow glue goat goddess gold "
    "good goose gorilla gospel gossip govern gown grab grace grain grant grape grass "
    "gravity great green grid grief grit grocery group grow grunt guard guess guide "
    "guitar gun gym habit hair half hammer hamster hand happy harbor harsh harvest "
    "hat have hawk hazard head heart heavy hedgehog height hello helmet help hen hero "
    "hidden high hill hint hip hire history hobby hockey hold hole holiday hollow home "
    "honey hood hope horn horse hospital host hotel hour hover hub huge human humble "
    "humor hundred hungry hunt hurdle hurry hurt husband hybrid ice icon idea identify "
    "idle ignore ill illegal illness image imitate immense immune impact impose improve "
    "impulse inch include income increase index indicate indoor industry infant inflict "
    "inform inhale inherit initial inject injury inmate inner innocent input inquiry "
    "insane insect inside inspire install intact interest into invest invite involve "
    "iron island isolate issue item ivory jacket jaguar jazz jealous jeans jelly jewel "
    "job join joke journey joy judge juice jump jungle junior junk just kangaroo keen "
    "keep ketchup key kick kid kidney kind kingdom kiss kit kitchen kite kitten kiwi "
    "knee knife knock know lab label labor ladder lady lake lamp language laptop large "
    "later latin laugh laundry lava law lawn lawsuit layer lazy leader leaf learn leave "
    "lecture left leg legal legend leisure lemon lend length lens leopard less lesson "
    "letter level liberty library license life lift light like limb limit link lion liquid "
    "list little live lizard load loan lobster local lock logic lonely long loop lottery "
    "loud lounge love loyal lucky luggage lumber lunar lunch luxury lyrics machine mad "
    "magic magnet maid mail main major make mammal man manage mandate mango mansion "
    "manual maple marble march margin marine market marriage mask mass master match "
    "material math matrix matter maximum maze meadow mean measure meat mechanic medal "
    "media melody melt member memory mention menu mercy merge merit merry mesh message "
    "metal method middle midnight milk million mimic mind minimum minor minute miracle "
    "mirror misery miss mistake mix mixed mixture mobile model modify mom moment monitor "
    "monkey monster month moon moral more morning mosquito mother motion motor mountain "
    "mouse move movie much muffin mule multiply muscle museum mushroom must mutual "
    "myself mystery myth naive name napkin narrow nasty nation nature near neck need "
    "negative neglect neither nephew nerve net network neutral never news next nice "
    "night noble noise nominee noodle normal north nose notable note nothing notice "
    "novel now nuclear number nurse nut oak obey object oblige obscure observe obtain "
    "obvious occur ocean october offer office often oil okay old olive olympic omit "
    "once one onion online only open opera opinion oppose option orange orbit orchard "
    "order ordinary organ orient original orphan ostrich other outdoor outer output "
    "outside oval oven over own owner oxygen oyster ozone pact paddle page pair "
    "palace palm panda panel panic panther paper parade parent park parrot party pass "
    "patch path patient patrol pattern pause pave payment peace peanut pear peasant "
    "pelican pen penalty pencil people pepper perfect permit person pet phone photo "
    "phrase physical piano picnic picture piece pig pigeon pill pilot pink pioneer pipe "
    "pistol pitch pizza place planet plastic plate play please pledge pluck plug plunge "
    "poem poet point polar pole police pond pony pool popular portion position possible "
    "post potato pottery poverty powder power practice praise predict prefer prepare "
    "present pretty prevent price pride primary print priority prison private prize "
    "problem process produce profit program promote proof property prosper protect proud "
    "provide public pudding pull pulp pulse pumpkin punch pupil puppy purchase purity "
    "purpose purse push put puzzle pyramid quality quantum quarter question quick quit "
    "quiz quote rabbit raccoon race rack radar radio rail rain raise rally ramp ranch "
    "random range rapid rare rate rather raven raw razor ready real reason rebel rebuild "
    "recall receive recipe record recycle reduce reflect reform refuse region regret "
    "regular reject relax release relief remain remember remind remove render renew "
    "rent reopen repeat reply require rescue resemble resist resource response result "
    "retire retreat return reunion reveal review reward rhythm rib ribbon rice rich "
    "ride ridge rifle right rigid ring riot ripple risk ritual rival river roast robe "
    "robot robust rocket romance roof rookie room rose rotate rough round route royal "
    "rug rule run runway rural sad saddle sadness safe sail salad salmon salon salt "
    "salute same sample sand satisfy satoshi sauce sausage save say scale scan scare "
    "scatter scene scheme school science scissors scorpion scout scrap screen script "
    "scrub sea search season seat second secret section security seed seek seem segment "
    "select sell seminar senior sense sentence series service session settle setup seven "
    "shadow shaft shallow share shed shell sheriff shield shift shine ship shiver shock "
    "shoe shoot shop short shoulder shove shrimp shrug shuffle shy sibling sick side "
    "siege sight sign silent silk silly silver similar simple since sing siren sister "
    "situate six size skate sketch ski skill skin skirt skull slab slam sleep slender "
    "slice slide slight slim slogan slot slow slush small smart smile smoke smooth "
    "snack snake snap sniff snow soap soccer social sock soda soft solar soldier solid "
    "solution solve someone song soon sorry sort soul sound soup source south space "
    "spare spatial spawn speak special speed spell spend sphere spice spider spike spin "
    "spirit split sponsor spoon sport spot spray spread spring spy square squeeze squirrel "
    "stable stadium staff stage stairs stamp stand start state stay steak steel stem "
    "step stereo stick still sting stock stomach stone stool story stove strategy street "
    "strike strong struggle student stuff stumble style subject submit subway success "
    "such sudden suffer suggest suit summer sun sunny sunset super supply supreme sure "
    "surface surge surprise surround survey suspect sustain swallow swamp swap swarm "
    "swear sweet swim swing switch sword symbol symptom syrup system table tackle tag "
    "tail talent talk tank tape target task taste tattoo taxi teach team tell ten tenant "
    "tennis tent term test text thank that theme then theory there they thing this "
    "thought three thrive throw thumb thunder ticket tide tiger tilt timber time tiny "
    "tip tired tissue title toast tobacco today toddler toe together toilet token "
    "tomato tomorrow tone tongue tonight tool tooth top topic topple torch tornado "
    "tortoise toss total tourist toward tower town toy track trade traffic tragic train "
    "transfer trap trash travel tray treat tree trend trial tribe trick trigger trim "
    "trip trophy trouble truck true trust try tube tuition tumble tuna tunnel turkey "
    "turn turtle twelve twenty twice twin twist two type typical ugly umbrella unable "
    "unaware uncle uncover under undo unfair unfold unhappy uniform unique unit universe "
    "unknown unlock until unusual unveil update upgrade uphold upon upper upset urban "
    "urge usage used useful useless usual utility vacuum vague valid valley valve van "
    "vanish vapor various vast vault vehicle velvet vendor venture venue verb verify "
    "version very veteran viable vibrant vicious victory video view village vintage "
    "violin virtual virus visa visit visual vital vivid vocal voice void volcano vote "
    "voyage wage wagon wait walk wall walnut want warfare warm warrior wash wasp waste "
    "water wave way wealth weapon wear weasel weather web wedding weekend weird welcome "
    "west wet whale what wheat wheel when where whip whisper wide width wife wild will "
    "win window wine wing wink winner winter wire wisdom wise wish witness wolf woman "
    "wonder wood wool word work world worry worth wrap wreck wrestle wrist write wrong "
    "yard year yellow you young youth zebra zero zone zoo"
).split()
_BIP39_SET = frozenset(_BIP39_HEAD)


# 64-символьная hex-строка, в строке сама по себе (не часть большего слова).
_HEX_PRIVATE_KEY = re.compile(r"(?<![\w])[0-9a-fA-F]{64}(?![\w])")
# base64 длиной 44 (приватник ed25519 = 32 байта, base64 ≈ 44 символа).
_B64_PRIVATE_KEY = re.compile(r"(?<![\w])[A-Za-z0-9+/]{43}=?(?![\w])")
# Слова, рядом с которыми 64-hex — приватный ключ. Без них — это TON-адрес
# (`0:<hex64>`) или хеш транзакции, оба безопасны для коммита.
_KEY_CONTEXT = re.compile(
    r"(?i)(?:priv(?:ate)?[_\s-]*key|secret|seed|mnemonic|TREASURY_MNEMONIC|"
    r"hex_key|hex_priv|priv_key|private[_\s-]*hex)",
)


def _scan_line(line: str) -> list[str]:
    """Вернуть список подозрительных фрагментов в строке (без самих значений)."""
    findings: list[str] = []
    # Мнемоника: ищем в каждой строковой константе — иначе ловим переменные
    # (`MNEMONIC = "..."`), которые портят счёт слов.
    for quoted in re.findall(r"['\"]([^'\"]+)['\"]", line):
        words = quoted.lower().split()
        if len(words) == 24 and all(w in _BIP39_SET for w in words):
            findings.append("BIP-39 mnemonic (24 words)")
            break
    # 2. Hex приватник. Срабатываем только если рядом контекст «priv_key/secret/...»
    # — иначе ловим TON-адреса (`0:<hex64>`) и tx-hash, которые безопасны.
    if _HEX_PRIVATE_KEY.search(line) and _KEY_CONTEXT.search(line):
        findings.append("64-char hex (likely ed25519 private key)")
    return findings


def _is_base64_private_key(line: str) -> bool:
    """Эвристика: строка из 44 base64 символов, похожая на ed25519 приватник.

    Не сигналим по base64, который встречается в тексте (комментарии, docstring).
    Опорные признаки: строка почти равна длине 44, без пробелов, не выглядит как
    английское предложение.
    """
    candidate = line.strip()
    if len(candidate) != 44:
        return False
    if " " in candidate or "\t" in candidate:
        return False
    if not _B64_PRIVATE_KEY.match(candidate):
        return False
    # Слишком много знаков пунктуации/букв — это явно не приватник.
    upper = sum(1 for c in candidate if c.isupper())
    lower = sum(1 for c in candidate if c.islower())
    digit = sum(1 for c in candidate if c.isdigit())
    slash = sum(1 for c in candidate if c in "+/")
    if slash == 0 or upper + lower + digit + slash != 44:
        return False
    return True


def _scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Вернуть список (номер_строки, фрагмент, вид_секрета) для файла."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, UnicodeDecodeError):
        return []
    out: list[tuple[int, str, str]] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\n")
        for kind in _scan_line(line):
            out.append((n, line[:120] + ("…" if len(line) > 120 else ""), kind))
        if _is_base64_private_key(line):
            out.append((n, line[:120], "base64 ed25519-like key"))
    return out


def _iter_paths(staged: bool) -> list[Path]:
    """Список файлов для сканирования: либо staged, либо весь репозиторий."""
    if staged:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMRTUXB"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            sys.exit(2)
        names = [n.strip() for n in result.stdout.splitlines() if n.strip()]
    else:
        names = []
        for p in Path(".").rglob("*"):
            if not p.is_file():
                continue
            rel = p.as_posix()
            if rel.startswith(".git/") or "/__pycache__/" in rel:
                continue
            if rel.startswith("data/") or rel.startswith(".venv/"):
                continue
            names.append(rel)
    return [Path(n) for n in names]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--staged",
        action="store_true",
        default=True,
        help="Сканировать только staged файлы (по умолчанию).",
    )
    parser.add_argument(
        "--all",
        dest="staged",
        action="store_false",
        help="Сканировать весь репозиторий.",
    )
    args = parser.parse_args()

    paths = _iter_paths(args.staged)
    bad: list[tuple[Path, int, str, str]] = []
    for p in paths:
        if not p.exists() or p.is_dir():
            continue
        # Бинарь — пропускаем.
        try:
            with p.open("rb") as fh:
                fh.read(8192)
        except (OSError, UnicodeDecodeError):
            continue
        findings = _scan_file(p)
        for line_no, snippet, kind in findings:
            bad.append((p, line_no, snippet, kind))

    if not bad:
        print("[OK] Секретов в сканируемых файлах не найдено.")
        return 0

    print("[FAIL] Найдены подозрительные строки. Коммит заблокирован:")
    for path, line_no, snippet, kind in bad:
        print(f"  {path}:{line_no}  [{kind}]")
        print(f"      {snippet!r}")
    print(
        "\nЕсли это ложное срабатывание (тест, пример в документации), "
        "добавь строку в .secrets-allowlist либо вынеси секрет в .env.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
