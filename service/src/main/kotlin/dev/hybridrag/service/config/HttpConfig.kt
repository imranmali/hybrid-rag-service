package dev.hybridrag.service.config

import dev.hybridrag.service.resilience.Dependency
import dev.hybridrag.service.resilience.DependencyHttpException
import org.springframework.http.HttpStatusCode
import org.springframework.http.MediaType
import org.springframework.http.client.JdkClientHttpRequestFactory
import org.springframework.stereotype.Component
import org.springframework.web.client.RestClient
import java.net.http.HttpClient
import java.time.Duration

/** Call types with their own read timeout (design D9). */
enum class CallType {
    EMBED,
    RERANK,
    LLM_ROUTE,
    LLM_SQLGEN,
    LLM_GENERATE,
    INFO,
}

/**
 * One shared JDK `HttpClient` (connect timeout 500 ms, HTTP/1.1) and one `RestClient` per call type whose read
 * timeout is the hard stage timeout, so no separate TimeLimiter thread is needed.
 */
@Component
class HttpClients(
    properties: RagProperties,
) {
    private val http =
        HttpClient
            .newBuilder()
            .version(HttpClient.Version.HTTP_1_1)
            .connectTimeout(Duration.ofMillis(properties.timeouts.connectMs))
            .build()

    private val clients =
        CallType.entries.associateWith { type ->
            val timeouts = properties.timeouts
            val readTimeout =
                when (type) {
                    CallType.EMBED -> timeouts.embedMs
                    CallType.RERANK -> timeouts.rerankMs
                    CallType.LLM_ROUTE -> timeouts.routeMs
                    CallType.LLM_SQLGEN -> timeouts.sqlGenerateMs
                    CallType.LLM_GENERATE -> timeouts.generateMs
                    CallType.INFO -> timeouts.infoMs
                }
            val factory = JdkClientHttpRequestFactory(http).apply { setReadTimeout(Duration.ofMillis(readTimeout)) }
            RestClient.builder().requestFactory(factory).build()
        }

    fun get(type: CallType): RestClient = clients.getValue(type)
}

/** POST a JSON body and return the 2xx body; non-2xx becomes [DependencyHttpException]. */
fun RestClient.postJson(
    dependency: Dependency,
    url: String,
    body: String,
    headers: Map<String, String> = emptyMap(),
): String =
    post()
        .uri(url)
        .contentType(MediaType.APPLICATION_JSON)
        .headers { target -> headers.forEach(target::set) }
        .body(body)
        .retrieve()
        .onStatus(HttpStatusCode::isError) { _, response ->
            throw DependencyHttpException(dependency, response.statusCode.value())
        }.body(String::class.java)
        .orEmpty()

/** GET returning the 2xx body; non-2xx becomes [DependencyHttpException]. */
fun RestClient.getJson(
    dependency: Dependency,
    url: String,
): String =
    get()
        .uri(url)
        .retrieve()
        .onStatus(HttpStatusCode::isError) { _, response ->
            throw DependencyHttpException(dependency, response.statusCode.value())
        }.body(String::class.java)
        .orEmpty()
