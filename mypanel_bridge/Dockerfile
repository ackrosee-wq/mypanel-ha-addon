ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install Python 3 and pip
RUN apk add --no-cache python3 py3-pip

# Create app directory
WORKDIR /app

# Install Python dependencies
COPY app/requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

# Copy application code
COPY app/ /app/
COPY run.sh /

# Make entry script executable
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
