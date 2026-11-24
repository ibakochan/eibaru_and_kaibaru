#!/bin/bash
set -euo pipefail
ts=$(date +%s)
for folder in karate fitness gym default cram eikaiwa dance swimming sports hair_salon nail_salon massage_salon; do
  url="https://storage.googleapis.com/sitepicsforkaibaru/layouts/$folder/hero.mp4?v=$ts"
  code=$(curl -sL -o /tmp/hero_verify.mp4 -w "%{http_code}" "$url")
  size=$(wc -c < /tmp/hero_verify.mp4)
  echo "$folder $code ${size}b"
done
