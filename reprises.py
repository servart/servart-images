"""Reprise des publications Instagram sur Threads et sur la page Facebook de Servart.

Ce fichier est recopié ici par le Pilote Servart (dossier servart-reseaux,
outils/pilote/depot/) : le modifier là-bas, pas ici.

Appelé par publier.py APRÈS la publication Instagram. Une erreur ici ne touche
jamais Instagram : au pire, la reprise est retentée au passage suivant (15 min),
3 essais, puis abandonnée avec une notification. Garde-fou doublons : avant de
publier, on regarde si la même légende est déjà en ligne.

Secrets du dépôt (un réseau est actif quand ses secrets sont là) :
  Threads  : TH_JETON, TH_USER_ID
  Facebook : FB_PAGE_ID, FB_PAGE_JETON (jeton de page, sans date d'expiration)
"""
import datetime, json, os, re, time, urllib.error, urllib.parse, urllib.request

THREADS = "https://graph.threads.net/v1.0/"
FACEBOOK = "https://graph.facebook.com/v23.0/"
LIMITE_THREADS = 500
LIEN = "servart.fr"
FENETRE_H = 6  # on ne reprend pas une publication plus vieille que ça (pas de rattrapage ancien)
ESSAIS_MAX = 3


def secret(nom):
    return os.environ.get(nom, "").strip()


def actifs():
    """Réseaux dont les secrets sont présents."""
    r = []
    if secret("TH_JETON") and secret("TH_USER_ID"):
        r.append("threads")
    if secret("FB_PAGE_ID") and secret("FB_PAGE_JETON"):
        r.append("facebook")
    return r


def appel(base, chemin, jeton, params=None, post=False):
    donnees = urllib.parse.urlencode(dict(params or {}, access_token=jeton))
    requete = (urllib.request.Request(base + chemin, data=donnees.encode()) if post
               else urllib.request.Request(base + chemin + "?" + donnees))
    try:
        with urllib.request.urlopen(requete, timeout=90) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(e.read().decode("utf-8", "replace")[:400]) from None


def debut(texte, n=60):
    return " ".join((texte or "").split())[:n]


# ---------- texte ----------

def sans_retours_forces(t):
    """Les légendes sont coupées en lignes courtes : on recolle les phrases, on garde listes et paragraphes."""
    sortie = []
    for ligne in t.splitlines():
        nouvelle = (not ligne.strip() or not sortie or not sortie[-1].strip()
                    or re.match(r"\s*([–\-•*]|\d+[.)]\s)", ligne)
                    or re.search(r"[.!?:…»)\U0001F300-\U0001FAFF]$", sortie[-1].rstrip()))
        if nouvelle:
            sortie.append(ligne)
        else:
            sortie[-1] = sortie[-1].rstrip() + " " + ligne.strip()
    return "\n".join(sortie)


def texte_threads(legende):
    """Légende d'Instagram → 500 caractères au plus, sans mots-dièse, avec servart.fr en clair."""
    t = re.sub(r"(?<!\S)#\w+", "", legende)             # Threads n'aime que 1 sujet : on les retire
    t = "\n".join(re.sub(r"[ \t]+", " ", l).strip() for l in t.splitlines())
    t = re.sub(r"\n{3,}", "\n\n", sans_retours_forces(t)).strip()
    if len(t) <= LIMITE_THREADS and LIEN in t:
        return t
    place = LIMITE_THREADS - len(LIEN) - 1
    if len(t) > place:
        coupe = t[:place]
        fin = max(coupe.rfind(c) for c in (".", "?", "!", "\n"))
        if fin >= place * 0.5:
            t = coupe[:fin + 1].strip()
        else:
            t = coupe[:coupe.rfind(" ")].rstrip(",;:") + "…"
    if t[-1:] in ";:,":
        t = t[:-1].rstrip() + "…"
    if LIEN not in t:
        t += "\n" + LIEN
    return t


def texte_facebook(legende):
    t = legende.strip()
    return t if LIEN in t else t + "\n\n" + LIEN


# ---------- Threads ----------

def threads_attendre(c, jeton, essais=40):
    for _ in range(essais):
        s = appel(THREADS, c, jeton, {"fields": "status,error_message"})
        if s.get("status") == "FINISHED":
            return
        if s.get("status") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Threads n'a pas pu préparer le média ({s.get('status')} {s.get('error_message', '')})")
        time.sleep(5)
    raise RuntimeError("Threads n'a pas fini de préparer le média")


def threads_deja(texte):
    jeton, user = secret("TH_JETON"), secret("TH_USER_ID")
    cle = debut(texte)
    for m in appel(THREADS, user + "/threads", jeton, {"fields": "id,text,permalink", "limit": "10"}).get("data", []):
        if debut(m.get("text")) == cle:
            return m["id"], m.get("permalink", "")
    return None


def threads_publier(rep):
    jeton, user = secret("TH_JETON"), secret("TH_USER_ID")
    texte = texte_threads(rep["legende"])
    deja = threads_deja(texte)
    if deja:
        return deja
    creer = lambda **p: appel(THREADS, user + "/threads", jeton, p, post=True)["id"]
    if rep.get("video"):
        c = creer(media_type="VIDEO", video_url=rep["video"], text=texte)
    elif len(rep["images"]) == 1:
        c = creer(media_type="IMAGE", image_url=rep["images"][0], text=texte)
    else:
        enfants = [creer(media_type="IMAGE", image_url=u, is_carousel_item="true") for u in rep["images"]]
        for e in enfants:
            threads_attendre(e, jeton)
        c = creer(media_type="CAROUSEL", children=",".join(enfants), text=texte)
    threads_attendre(c, jeton)
    pub = appel(THREADS, user + "/threads_publish", jeton, {"creation_id": c}, post=True)["id"]
    try:
        lien = appel(THREADS, pub, jeton, {"fields": "permalink"}).get("permalink", "")
    except Exception:
        lien = ""
    return pub, lien


# ---------- Facebook ----------

def facebook_deja(texte):
    page, jeton = secret("FB_PAGE_ID"), secret("FB_PAGE_JETON")
    cle = debut(texte)
    for m in appel(FACEBOOK, page + "/posts", jeton, {"fields": "id,message,permalink_url", "limit": "10"}).get("data", []):
        if debut(m.get("message")) == cle:
            return m["id"], m.get("permalink_url", "")
    return None


def facebook_publier(rep):
    page, jeton = secret("FB_PAGE_ID"), secret("FB_PAGE_JETON")
    texte = texte_facebook(rep["legende"])
    deja = facebook_deja(texte)
    if deja:
        return deja
    if rep.get("video"):
        ident = appel(FACEBOOK, page + "/videos", jeton, {"file_url": rep["video"], "description": texte}, post=True)["id"]
    elif len(rep["images"]) == 1:
        r = appel(FACEBOOK, page + "/photos", jeton, {"url": rep["images"][0], "caption": texte}, post=True)
        ident = r.get("post_id") or r["id"]
    else:
        photos = [appel(FACEBOOK, page + "/photos", jeton, {"url": u, "published": "false"}, post=True)["id"]
                  for u in rep["images"]]
        ident = appel(FACEBOOK, page + "/feed", jeton, {
            "message": texte,
            "attached_media": json.dumps([{"media_fbid": i} for i in photos])}, post=True)["id"]
    try:
        lien = appel(FACEBOOK, ident, jeton, {"fields": "permalink_url"}).get("permalink_url", "")
        if lien.startswith("/"):
            lien = "https://www.facebook.com" + lien
    except Exception:
        lien = ""
    return ident, lien


def facebook_story(url):
    """Story de la page Facebook (photo_stories) : la photo est envoyée non publiée, puis posée en story."""
    page, jeton = secret("FB_PAGE_ID"), secret("FB_PAGE_JETON")
    photo = appel(FACEBOOK, page + "/photos", jeton, {"url": url, "published": "false"}, post=True)["id"]
    rep = appel(FACEBOOK, page + "/photo_stories", jeton, {"photo_id": photo}, post=True)
    return str(rep.get("post_id") or photo)


PUBLIER = {"threads": threads_publier, "facebook": facebook_publier}
NOMS = {"threads": "Threads", "facebook": "Facebook"}


# ---------- orchestration ----------

def a_faire(rec, maintenant):
    """Réseaux à tenter pour une publication rangée dans publie/ (vide si rien à faire)."""
    if not rec.get("reprise") or not rec.get("publiee_le"):
        return []
    age = maintenant - datetime.datetime.fromisoformat(rec["publiee_le"])
    if age > datetime.timedelta(hours=FENETRE_H):
        return []
    return [r for r in actifs() if not rec.get(r)]


def reprendre(rec, maintenant):
    """Tente Threads puis Facebook. Modifie rec ; renvoie les messages à notifier. Ne lève jamais d'exception."""
    messages = []
    for reseau in a_faire(rec, maintenant):
        try:
            ident, lien = PUBLIER[reseau](rec["reprise"])
            rec[reseau] = {"id": ident, "lien": lien, "le": maintenant.isoformat(timespec="minutes")}
            messages.append(f"{NOMS[reseau]} : {lien or 'en ligne'}")
        except Exception as e:
            essais = rec.setdefault("reprise_essais", {})
            essais[reseau] = essais.get(reseau, 0) + 1
            rec.setdefault("reprise_erreurs", {})[reseau] = str(e)[:300]
            if essais[reseau] >= ESSAIS_MAX:
                rec[reseau] = {"abandon": str(e)[:300]}
                messages.append(f"{NOMS[reseau]} ABANDONNÉ après {ESSAIS_MAX} essais : {str(e)[:150]}")
    return messages


def renouveler_threads():
    """Prolonge le jeton Threads de 60 jours et renvoie le nouveau (ou lève une erreur)."""
    url = ("https://graph.threads.net/refresh_access_token?grant_type=th_refresh_token&access_token="
           + secret("TH_JETON"))
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)["access_token"]
