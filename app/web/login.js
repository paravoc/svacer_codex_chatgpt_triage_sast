if (new URLSearchParams(location.search).has("error")) {
  document.querySelector(".login-error").classList.add("visible");
}
