# Base state (state_0) of the Chatwoot automation task: Chatwoot CE with the automation backend removed.
# Built on the shared image in tasks/_images/chatwoot-ce, which must already exist on the Docker host.
FROM csf/chatwoot-ce:3564ec1e

# Remove the milestones' code. The patch is bind-mounted for this one step, so it never lands in an
# image layer the agent could read.
RUN --mount=type=bind,source=base.patch,target=/tmp/csf/base.patch \
    cd /app && git apply --whitespace=nowarn /tmp/csf/base.patch

COPY csf-collect /usr/local/bin/csf-collect

# Rebuild both databases from the stripped schema and rebuild the test-env assets. db/schema.rb is left as
# upstream wrote it: a load + dump round trip on Postgres 16 rewrites 5 unrelated index lines and the
# Schema[7.1] header, exactly as it does on the unmodified tree (checked when the base state was cut).
RUN csf-services start \
 && bundle exec rails db:drop db:create db:schema:load db:seed \
 && RAILS_ENV=test bundle exec rails db:drop db:create db:schema:load \
 && RAILS_ENV=test bin/vite build \
 && csf-services stop

# A fresh one-commit repository of the base tree, with no upstream history. /app/.git is the agent's to
# use; the copy in /opt/csf is the grader's fixed reference for csf-collect.
RUN cd /app \
 && git init -q \
 && git add -A \
 && git -c user.name=Chatwoot -c user.email=dev@chatwoot.local commit -q -m "Chatwoot" \
 && mkdir -p /opt/csf \
 && cp -a .git /opt/csf/base.git
