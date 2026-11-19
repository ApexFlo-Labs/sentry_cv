set -eu
install -m 644 /control/kobuk.asc /usr/share/keyrings/sentry-kobuk.asc
printf '%s\n' 'deb [signed-by=/usr/share/keyrings/sentry-kobuk.asc] https://snapshot.ppa.launchpadcontent.net/kobuk-team/intel-graphics/ubuntu/20260624T030400Z noble main' > /etc/apt/sources.list.d/sentry-kobuk.list
rm -f /etc/apt/sources.list.d/intel-gpu-noble.list
apt-get update

DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends intel-opencl-icd=26.18.38308.4-1~24.04~ppa1 intel-ocloc=26.18.38308.4-1~24.04~ppa1 libze-intel-gpu1=26.18.38308.4-1~24.04~ppa1 libze1=1.28.2-1~24.04~ppa1 libigc2=2.34.4-1260~24.04 libigdfcl2=2.34.4-1260~24.04 libigdgmm12=22.10.0-1~24.04~ppa1 intel-media-va-driver-non-free=26.2.2-1~24.04~ppa1 libva2=2.23.0-1~24.04~ppa5 libva-drm2=2.23.0-1~24.04~ppa5 libva-x11-2=2.23.0-1~24.04~ppa5 libva-wayland2=2.23.0-1~24.04~ppa5
mkdir -p /opt/sentry-driver-control
dpkg-query -W > /opt/sentry-driver-control/dpkg-after.tsv
ldconfig
apt-get clean
rm -rf /var/lib/apt/lists/*
