package dev.hybridrag.service

import dev.hybridrag.service.config.RagProperties
import org.springframework.boot.autoconfigure.SpringBootApplication
import org.springframework.boot.context.properties.EnableConfigurationProperties
import org.springframework.boot.runApplication

@SpringBootApplication
@EnableConfigurationProperties(RagProperties::class)
class Application

fun main(args: Array<String>) {
    runApplication<Application>(*args)
}
