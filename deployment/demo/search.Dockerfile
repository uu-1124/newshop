FROM elasticsearch:8.18.0
RUN curl -fLsS https://get.infini.cloud/elasticsearch/analysis-ik/8.18.0 -o /tmp/analysis-ik.zip \
    && echo '3ff14b395289ad8e00e310b6514902512be43b63c7ac73cf9db92790e556b206  /tmp/analysis-ik.zip' | sha256sum -c - \
    && elasticsearch-plugin install --batch file:///tmp/analysis-ik.zip \
    && rm /tmp/analysis-ik.zip
