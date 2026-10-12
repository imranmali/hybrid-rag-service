FROM eclipse-temurin:25-jdk AS build
WORKDIR /workspace
COPY gradlew settings.gradle.kts build.gradle.kts ./
COPY gradle gradle
RUN ./gradlew --no-daemon dependencies >/dev/null
COPY src src
RUN ./gradlew --no-daemon bootJar -x test

FROM eclipse-temurin:25-jre
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}
ENV JAVA_TOOL_OPTIONS=-javaagent:/otel/opentelemetry-javaagent.jar
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    useradd --system --uid 10001 app && \
    mkdir /otel && \
    curl -fsSL https://github.com/open-telemetry/opentelemetry-java-instrumentation/releases/download/v2.31.1/opentelemetry-javaagent.jar \
      -o /otel/opentelemetry-javaagent.jar
COPY --from=build /workspace/build/libs/*.jar /app/app.jar
RUN chown -R app:app /app /otel
USER app
EXPOSE 8080
ENTRYPOINT ["java", "-XX:MaxRAMPercentage=75", "-jar", "/app/app.jar"]
