"""Thin Kubernetes API wrapper. Returns plain dicts (API JSON shape) so logic can be tested with a fake."""
import yaml
from kubernetes import client, config
from kubernetes.client.rest import ApiException


class Kube:
    def __init__(self):
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self.api = client.ApiClient()
        self.batch = client.BatchV1Api(self.api)
        self.core = client.CoreV1Api(self.api)

    def _d(self, obj):
        return self.api.sanitize_for_serialization(obj)

    def read_runners_yaml(self, namespace, name="llm-bench-runners"):
        cm = self.core.read_namespaced_config_map(name, namespace)
        return yaml.safe_load(cm.data["runners.yaml"])

    def create_job(self, namespace, body):
        return self._d(self.batch.create_namespaced_job(namespace, body))

    def get_job(self, namespace, name):
        try:
            return self._d(self.batch.read_namespaced_job(name, namespace))
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def list_jobs(self, namespace, selector):
        return self._d(self.batch.list_namespaced_job(namespace, label_selector=selector))["items"]

    def delete_job(self, namespace, name):
        try:
            self.batch.delete_namespaced_job(name, namespace, propagation_policy="Background")
        except ApiException as e:
            if e.status != 404:
                raise

    def list_pods(self, namespace, selector):
        return self._d(self.core.list_namespaced_pod(namespace, label_selector=selector))["items"]

    def pod_log(self, namespace, name, container=None, tail=200):
        try:
            return self.core.read_namespaced_pod_log(name, namespace, container=container, tail_lines=tail)
        except ApiException as e:
            if e.status in (400, 404):
                return ""
            raise

    def list_events(self, namespace, field_selector):
        return self._d(self.core.list_namespaced_event(namespace, field_selector=field_selector))["items"]

    def get_service(self, namespace, name):
        try:
            return self._d(self.core.read_namespaced_service(name, namespace))
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def create_service(self, namespace, body):
        try:
            return self._d(self.core.create_namespaced_service(namespace, body))
        except ApiException as e:
            if e.status == 409:
                return self.get_service(namespace, body["metadata"]["name"])
            raise

    def delete_service(self, namespace, name):
        try:
            self.core.delete_namespaced_service(name, namespace)
        except ApiException as e:
            if e.status != 404:
                raise

    def list_quotas(self, namespace):
        return self._d(self.core.list_namespaced_resource_quota(namespace))["items"]
