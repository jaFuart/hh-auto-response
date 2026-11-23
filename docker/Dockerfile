# Версии пакета Playwright и браузеров должны совпадать.
FROM python:3.12-slim-bookworm
ARG PLAYWRIGHT_VERSION=1.64.1
ARG UC_CHROME_VERSION=158.0.8127.36

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    HH_AUTO_HOME=/data \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HH_SB_BINARY_LOCATION=cft \
    HH_SB_CHROME_VERSION=${UC_CHROME_VERSION} \
    TZ=Europe/Moscow \
    LANG=ru_RU.UTF-8

# Playwright использует headless-shell; SeleniumBase UC запускает полный Chrome во внутреннем Xvfb.
# Xvfb включается только выбранным backend, без VNC и опубликованных портов.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu fonts-liberation locales tzdata xvfb xauth \
 && sed -i 's/# ru_RU.UTF-8/ru_RU.UTF-8/' /etc/locale.gen && locale-gen \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt "playwright==${PLAYWRIGHT_VERSION}" \
 && pip check
RUN python -m playwright install --with-deps --only-shell chromium \
 && rm -rf /var/lib/apt/lists/*

# Полный Chrome и его UC-драйвер устанавливаются при сборке, без загрузок при запуске.
RUN apt-get update \
 && apt-get install -y --no-install-recommends unzip libtk8.6 \
 && rm -rf /var/lib/apt/lists/* \
 && seleniumbase get cft "${UC_CHROME_VERSION}" \
 && seleniumbase get uc_driver "${UC_CHROME_VERSION}" \
 && python -c "import os, shutil, subprocess; from pathlib import Path; from seleniumbase import drivers; from seleniumbase.drivers import cft_drivers; from seleniumbase.undetected.patcher import Patcher; directory=Path(drivers.__file__).parent; cft=Path(cft_drivers.__file__).parent; browsers=list(cft.glob('chrome-*/chrome')); assert len(browsers)==1, 'Expected one installed Chrome for Testing binary'; chrome=browsers[0]; source=directory/'uc_driver'; target=cft/'uc_driver'; assert source.is_file(), 'Missing installed UC driver'; shutil.copy2(source,target); paths=(source,target); [Patcher(executable_path=str(path)).auto() for path in paths]; assert all(Patcher(executable_path=str(path)).is_binary_patched() for path in paths), 'UC driver patch failed'; expected=os.environ['HH_SB_CHROME_VERSION']; assert expected in subprocess.check_output([str(chrome),'--version'],text=True), 'Chrome version mismatch'; assert expected in subprocess.check_output([str(target),'--version'],text=True), 'UC driver version mismatch'; Path('/usr/local/bin/chrome-for-testing').symlink_to(chrome); print('Pinned Chrome for Testing and matching UC drivers installed and verified')"

# При запуске используем проверенный локальный бинарник без разрешения псевдонима cft.
ENV HH_SB_BINARY_LOCATION=/usr/local/bin/chrome-for-testing

COPY hh_auto ./hh_auto
COPY scripts ./scripts
COPY config.example.toml ./
COPY config.openai.example.toml ./
COPY config.server.example.toml ./
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME /data
ENTRYPOINT ["/entrypoint.sh"]
CMD ["server"]
