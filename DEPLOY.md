# Deploy Sift to Railway

## One-time prep

```bash
cd ~/Desktop/Sift
git init
git add .
git commit -m "initial commit"
# Create a new empty repo on GitHub (call it "sift" or whatever)
git remote add origin git@github.com:<you>/sift.git
git push -u origin main
```

## Railway

```bash
railway login
railway init                  # name the project "sift"
railway link                  # link this folder to the project
railway up                    # first build & deploy
```

Then in the Railway dashboard:

1. **Variables → New Variable**
   - `S2_API_KEY = s2k-...`
   - `OPENALEX_KEY = ...`
   - `PORT = 8080` (auto-set, but explicit doesn't hurt)
2. **Volumes → New Volume**
   - mount path: `/data`
   - size: 5 GB (DBLP ~2.5 GB, ACL ~60 MB, headroom)
3. **Settings → Networking → Generate Domain** for a `*.up.railway.app` URL to test, then add your custom domain (CNAME → the Railway target it gives you).

## Populate the DBLP / ACL databases on the volume

The volume is empty on first deploy. Slate runs fine without it (slower, more
"not_found" from online rate limits). To populate:

```bash
# Shell into the running container:
railway shell

# Inside the container:
curl -sSf https://hallucinator.science/install-cli.sh | sh
~/.cargo/bin/hallucinator-cli update-dblp /data/dblp.db    # ~25 min
~/.cargo/bin/hallucinator-cli update-acl  /data/acl.db     # ~2 min
exit
```

The DBs persist on the volume across deploys. Refresh DBLP every ~30 days
with the same command.

## Subsequent deploys

```bash
git push
# Railway auto-builds and deploys on every push.
```

## Local testing of the Docker image

```bash
docker build -t sift .
docker run -p 8080:8080 \
   -e S2_API_KEY=... -e OPENALEX_KEY=... \
   -v ~/.local/share/hallucinator:/data \
   sift
# Open http://localhost:8080
```
